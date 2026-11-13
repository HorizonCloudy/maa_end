"""陪聊 + 卡关辅助运行时（插件侧）。

职责
----
1. **陪聊默认**：默认只读、被动——不订阅、不占资源，问答走 router 的 llm_tool。
2. **卡关辅助自动触发**：后台订阅 ZMDLogs 事件流 → 战斗切分 → 四类归因 →
   分层输出 → 仅在真正卡关时推送建议。

设计约束（对齐 `game_plugin_template/docs/范式.md`）
- **只读**：本模块不点击、不注入、不启动游戏自动化。它只订阅本地回环 WS / 读日志文件。
- **零第三方依赖**：纯标准库——WS 客户端是手写的 RFC 6455 子集（宿主是冻结
  exe，不能假定装了 websockets 包），与 `labeler/cline` 同源逻辑。
- **失败隔离**：任何异常都不得拖垮插件——循环体包 try/except，只在日志如实报告。
- **降级有序**：WS 与 `logs/` 双源每轮合并；都不可用 → 静默待命（不假装在工作）。
- **单例套组模式**：本模块不切任何全局模式，无副作用需要还原。

事件来源与送达率（2026-10-12 修整）
----------------------------------
依据：v7 选型报告 §S9 / verify-hw-tier-staged-plan-correction（「assist 管线目前
只送达约 29% 的事件」）与 `docs/ws-reverse-engineering.md` §7.8（服务端不重放）。

旧实现的问题：
1. 每个 poll 周期只连 WS 听 2 秒（周期 ≈7 s ⇒ 送达率 ~29%），断连窗口内的
   事件**永久丢失**——ZMDLogs 只发不收、断线不重放；
2. logs 降级只在「WS 一个事件都没给」时才启用，两源从未真正合并；
3. 每轮 poll 独立切分战斗 ⇒ 同一场战斗被 poll 边界切成碎片，`started_at_ms` /
   `event_count` 全错，`FailureCounter` 的连续计数被碎片反复冲刷。

现改为：
1. **持久订阅**：`_WsThread` 后台长连（断线指数退避重连 0.5→10 s），事件持续入缓冲；
2. **logs 增量尾读**：按文件偏移只解析新追加的完整行（首见回看 256 KiB 尾部；
   半行回退；截断重读）。**logs 是全量真值**（每个事件都落 ndjson），WS 只是
   提前量——所以每轮 poll 两边都读，指纹去重后合并，送达率不再依赖连接占空比；
3. **跨轮缓冲**：事件攒到「战斗结束标记」（`is_in_battle=false`）才整体交给
   `split_battles`，一场战斗一份完整记录；30 分钟 / 2 万条强制冲刷兜底
   （ZMDLogs 中途崩溃、结束标记永远不来时不至于无限等待）。

协议细节见 `docs/ws-reverse-engineering.md`。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ---- 内联核心逻辑（避免插件对外部 workspace 的路径依赖）-------------------
# 说明：`labeler/cline/*` 是本项目的算法源。为让插件可独立加载，这里**不 import
# 那个包**（它在插件目录之外，跨目录 import 一搬家即坏——范式文档 §4.2 的反面教材）。
# 插件侧只依赖本文件 + `assist_rules.py`（同目录），二者与 labeler 保持同构。
from .assist_rules import (  # noqa: E402, F401 - battle_from_events 供 api/assist.py 转口
    BATTLE_STATE_TYPE,
    MACRO_BY_LABEL,
    FailureCounter,
    RulesConfig,
    StuckConfig,
    battle_failed,
    battle_from_events,
    classify_events,
    detect_stuck,
    split_battles,
)

DEFAULT_WS_PORT = 29325
DEFAULT_WS_URL = f"ws://127.0.0.1:{DEFAULT_WS_PORT}/ws"

#: ZMDLogs logs 目录的常见位置（降级源自动探测；router 侧共用这一份清单）
LOG_DIR_CANDIDATES = (
    "%LOCALAPPDATA%/ZMDLogs/logs",
    "%APPDATA%/ZMDLogs/logs",
    "%LOCALAPPDATA%/Programs/ZMDLogs/logs",
    "%USERPROFILE%/ZMDLogs/logs",
)

#: logs 首见文件只回看这么多尾部字节（避免启动时全量解析大文件）
LOG_TAIL_BYTES = 262_144

#: 同时跟踪的 session 文件数（按 mtime 取最新；活跃 session 永远在其中）
LOG_TRACK_FILES = 4

#: 首见文件的回看新鲜度门槛（秒）：mtime 超过这个年龄的历史抓包不再回看尾部。
#: 与下面 PENDING_FLUSH_SECONDS 数值相同、语义不同（一个是文件年龄，一个是缓冲时长）。
LOG_FRESH_SECONDS = 30 * 60.0

#: 跨轮缓冲的强制冲刷阈值（防内存无限涨 / 判定无限等）
PENDING_FLUSH_EVENTS = 20_000
PENDING_FLUSH_SECONDS = 30 * 60.0


def _has_session_file(root: Path) -> bool:
    try:
        return next(root.rglob("session_*.ndjson"), None) is not None
    except OSError:
        return False


def find_logs_dir(explicit: str = "") -> Path | None:
    """定位 ZMDLogs 的 logs 目录；找不到返回 None（不猜、不假装可用）。

    显式路径只要求「目录存在」（用户可能指向一个还没写出 session 的空目录，
    ZMDLogs 之后会往里写）；自动探测的候选位置则要求**真的有 session_*.ndjson**
    ——空目录不该被当成可用来源。
    """
    if explicit:
        cand = Path(os.path.expandvars(str(explicit))).expanduser()
        if cand.is_dir():
            return cand
    for pattern in LOG_DIR_CANDIDATES:
        cand = Path(os.path.expandvars(pattern.replace("/", os.sep))).expanduser()
        try:
            if cand.is_dir() and _has_session_file(cand):
                return cand
        except OSError:
            continue
    return None


# ---------------------------------------------------------------- 双配置（A/B 档）

#: 可用的配置档
PROFILES: tuple[str, ...] = ("A", "B")

#: 三项参数 → 扁平键里的短名
_PARAM_SHORT: dict[str, str] = {
    "assist_mode": "mode",
    "confidence_threshold": "confidence",
    "consecutive_fail_threshold": "consecutive",
}

#: 三项参数的合法档位（非法值吸附到最近的合法档，不静默取默认）
MODES: tuple[str, ...] = ("宏观建议", "精细建议")
CONFIDENCES: tuple[float, ...] = (0.70, 0.80, 0.85, 0.90)
CONSECUTIVES: tuple[int, ...] = (2, 3, 5)


def profile_key(profile: str, param: str) -> str:
    """某档某参数对应的扁平配置键，如 ('B','assist_mode') → assist_profile_b_mode。"""
    short = _PARAM_SHORT.get(param)
    if not short:
        raise KeyError(f"未知参数: {param}")
    return f"assist_profile_{profile.strip().lower()}_{short}"


def _norm_mode(v: Any, fallback: str = "宏观建议") -> str:
    s = str(v).strip() if v is not None else ""
    return s if s in MODES else fallback


def _norm_conf(v: Any, fallback: float = 0.85) -> float:
    try:
        f = float(v)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if not f == f:            # NaN
        return fallback
    return f if f in CONFIDENCES else min(CONFIDENCES, key=lambda c: abs(c - f))


def _norm_consec(v: Any, fallback: int = 3) -> int:
    try:
        i = int(v)            # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return i if i in CONSECUTIVES else min(CONSECUTIVES, key=lambda c: abs(c - i))


def _norm_poll(v: Any) -> float:
    try:
        p = float(v)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 5.0
    if not p == p:            # NaN
        return 5.0
    return max(1.0, min(60.0, p))


@dataclass(slots=True)
class AssistConfig:
    """三项可配置参数（与 plugin.toml 的 [maa_assist] 段、面板 Select 对应）。

    2026-11-02 新增**双配置（A/B 档）**：两套独立参数 + 运行时热切换，
    想法来自 JayziLi/endfield 的「两套独立配置、可分别绑定、算法与参数
    可在运行时热切换」（只取其配置交互部分，不涉及该仓库的输入注入）。

    存储用**扁平键**（plugin.toml 是扁平结构，不用嵌套 dict）：
        assist_profile            = "A" | "B"      当前生效档
        assist_profile_a_mode     = 宏观建议/精细建议
        assist_profile_a_confidence / assist_profile_b_confidence
        assist_profile_a_consecutive / assist_profile_b_consecutive
    未配置某档时回落到旧的扁平键（assist_mode 等），老配置不受影响。
    """

    assist_mode: str = "宏观建议"
    confidence_threshold: float = 0.85
    consecutive_fail_threshold: int = 3
    #: 轮询间隔（秒）：后台循环多久收一次双源缓冲（WS 线程独立持续收）
    poll_seconds: float = 5.0
    #: 是否启用卡关辅助（关闭则完全不动）
    enabled: bool = True
    #: ZMDLogs 的 logs 目录（降级来源）；空=自动探测
    log_dir: str = ""
    #: WS 地址；空=默认
    ws_url: str = ""
    #: 当前生效的配置档
    profile: str = "A"
    #: 两档各自的参数 {"A": {...}, "B": {...}}，供面板展示与切换
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ---- 档位 ----

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None) -> "AssistConfig":
        raw = settings or {}
        legacy = cls(
            assist_mode=_norm_mode(raw.get("assist_mode")),
            confidence_threshold=_norm_conf(raw.get("confidence_threshold")),
            consecutive_fail_threshold=_norm_consec(raw.get("consecutive_fail_threshold")),
            poll_seconds=_norm_poll(raw.get("assist_poll_seconds")),
            enabled=bool(raw.get("assist_enabled", True)),
            log_dir=str(raw.get("assist_log_dir") or ""),
            ws_url=str(raw.get("assist_ws_url") or ""),
        )
        prof = str(raw.get("assist_profile") or "A").strip().upper()
        if prof not in PROFILES:
            prof = "A"

        # 两档都解析出来（缺的用旧扁平值兜底），这样面板能同时显示两套
        profiles: dict[str, dict[str, Any]] = {}
        for p in PROFILES:
            profiles[p] = {
                "assist_mode": _norm_mode(raw.get(profile_key(p, "assist_mode")),
                                          legacy.assist_mode),
                "confidence_threshold": _norm_conf(raw.get(profile_key(p, "confidence_threshold")),
                                                   legacy.confidence_threshold),
                "consecutive_fail_threshold": _norm_consec(
                    raw.get(profile_key(p, "consecutive_fail_threshold")),
                    legacy.consecutive_fail_threshold),
            }
        active = profiles[prof]
        return cls(
            assist_mode=active["assist_mode"],
            confidence_threshold=active["confidence_threshold"],
            consecutive_fail_threshold=active["consecutive_fail_threshold"],
            poll_seconds=legacy.poll_seconds,
            enabled=legacy.enabled,
            log_dir=legacy.log_dir,
            ws_url=legacy.ws_url,
            profile=prof,
            profiles=profiles,
        )

    def as_settings_patch(self, profile: str | None = None) -> dict[str, Any]:
        """把某档（默认当前档）导出成可写回配置的扁平键。"""
        p = (profile or self.profile).upper()
        v = self.profiles.get(p) or {
            "assist_mode": self.assist_mode,
            "confidence_threshold": self.confidence_threshold,
            "consecutive_fail_threshold": self.consecutive_fail_threshold,
        }
        return {
            "assist_profile": p,
            profile_key(p, "assist_mode"): v["assist_mode"],
            profile_key(p, "confidence_threshold"): v["confidence_threshold"],
            profile_key(p, "consecutive_fail_threshold"): v["consecutive_fail_threshold"],
        }


@dataclass(slots=True)
class AssistEvent:
    """一次卡关判定 → 一条待推送的建议。"""

    ts: float
    session_id: str
    signal: str
    label: str
    confidence: float
    macro: str
    fine: str | None
    fine_suppressed_reason: str | None
    consecutive_count: int
    #: 场景佐证（可选）：判定产出时游戏前台的画面类别（仅前台采集；None=未采集/非前台）
    scene_evidence: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "session_id": self.session_id,
            "signal": self.signal,
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "macro": self.macro,
            "fine": self.fine,
            "fine_suppressed_reason": self.fine_suppressed_reason,
            "consecutive_count": self.consecutive_count,
            "scene_evidence": self.scene_evidence,
        }

    def text(self) -> str:
        parts = [self.macro]
        if self.fine:
            bullets = "\n".join(f"- {x}" for x in self.fine.split("\n") if x.strip())
            parts.append(f"精细建议：\n{bullets}")
        elif self.fine_suppressed_reason:
            parts.append(f"（精细建议暂不给出：{self.fine_suppressed_reason}）")
        return "\n".join(parts)


# ============================================================ WS 持久订阅客户端


class _FrameReader:
    """从 socket 读 RFC 6455 帧（带缓冲；支持 16/64 位长度、掩码、分片）。"""

    def __init__(self, sock: socket.socket, initial: bytes = b"") -> None:
        self._sock = sock
        self._buf = bytearray(initial)

    def _need(self, n: int) -> None:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("WS 对端关闭")
            self._buf += chunk

    def read_frame(self) -> tuple[bool, int, bytes]:
        """读一帧，返回 ``(fin, opcode, payload)``。"""
        self._need(2)
        b0 = self._buf[0]
        b1 = self._buf[1]
        del self._buf[:2]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            self._need(2)
            length = int.from_bytes(bytes(self._buf[:2]), "big")
            del self._buf[:2]
        elif length == 127:
            self._need(8)
            length = int.from_bytes(bytes(self._buf[:8]), "big")
            del self._buf[:8]
        if length > 32 * 1024 * 1024:
            raise ConnectionError(f"WS 帧长度异常：{length}")
        if masked:  # RFC：服务端→客户端帧不应掩码，防御性兼容
            self._need(4 + length)
            mask = bytes(self._buf[:4])
            payload = bytearray(self._buf[4:4 + length])
            del self._buf[:4 + length]
            for i in range(length):
                payload[i] ^= mask[i & 3]
            return fin, opcode, bytes(payload)
        self._need(length)
        payload = bytes(self._buf[:length])
        del self._buf[:length]
        return fin, opcode, payload


class _WsThread:
    """持久订阅 ``ws://127.0.0.1:29325/ws`` 的后台线程。

    为什么手写而不用 websockets 库：插件宿主是冻结 exe，第三方包不可假定
    存在；本用例是回环、无鉴权、服务端只推不收的单连接文本流，RFC 6455
    的这个子集 ~150 行足够可靠。

    连接语义：断线后指数退避重连（0.5 s → 10 s 封顶）；文本帧交给回调。
    socket 读超时 45 s——ZMDLogs（websockets 库服务端）默认每 20 s 发协议层
    ping，本线程即时回 pong；连续两个周期收不到任何帧即视为死连接并重连。
    **服务端不重放历史**：断线窗口内的事件由 ZoneReader 的 logs 增量读补齐。
    """

    _READ_TIMEOUT = 45.0
    _CONNECT_TIMEOUT = 3.0
    _BACKOFF_START = 0.5
    _BACKOFF_MAX = 10.0

    def __init__(self, url: str, on_payload: Callable[[str], None]) -> None:
        m = re.match(r"^ws://([^/:]+):(\d+)(/.*)?$", (url or "").strip() or DEFAULT_WS_URL)
        if m:
            self._host = m.group(1)
            self._port = int(m.group(2))
            self._path = m.group(3) or "/ws"
        else:
            self._host, self._port, self._path = "127.0.0.1", DEFAULT_WS_PORT, "/ws"
        self._on_payload = on_payload
        self._stop = threading.Event()
        self._owner_stopped = False
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        #: 对外只读状态（state() 汇报用）
        self.connected = False
        self.sessions = 0
        self.last_error = ""

    # ---- 生命周期 ----
    def ensure_started(self) -> None:
        """懒启动（首个 poll 时拉起）；owner 调用过 stop() 则永不复活。"""
        if self._owner_stopped:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="maaend-assist-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._owner_stopped = True
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # ---- 重连主循环 ----
    def _run(self) -> None:
        backoff = self._BACKOFF_START
        while not self._stop.is_set():
            try:
                self._session()
                backoff = self._BACKOFF_START
            except Exception as exc:  # noqa: BLE001 - 重连循环永不冒泡
                self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            if self._stop.is_set():
                break
            self._stop.wait(backoff)
            backoff = min(backoff * 2.0, self._BACKOFF_MAX)

    def _session(self) -> None:
        """一次「连接 → 收帧直到断开」的会话。"""
        sock = socket.create_connection(
            (self._host, self._port), timeout=self._CONNECT_TIMEOUT)
        self._sock = sock
        try:
            sock.settimeout(self._READ_TIMEOUT)
            leftover = self._handshake(sock)
            self.connected = True
            self.sessions += 1
            reader = _FrameReader(sock, leftover)
            frag_buf: list[bytes] = []
            while not self._stop.is_set():
                fin, opcode, payload = reader.read_frame()
                if opcode == 0x9:  # ping → 立即回 pong（不回会被服务端踢）
                    self._send_masked(sock, 0xA, payload[:125])
                    continue
                if opcode == 0xA:  # pong（本客户端不发 ping，忽略）
                    continue
                if opcode == 0x8:  # close → 尽力回 close，结束本会话
                    try:
                        self._send_masked(sock, 0x8, b"")
                    except OSError:
                        pass
                    raise ConnectionError("服务端关闭连接")
                if opcode in (0x1, 0x2):
                    if fin:
                        frag_buf = []
                        self._on_payload(payload.decode("utf-8", errors="replace"))
                    else:
                        frag_buf = [payload]
                elif opcode == 0x0:  # continuation
                    frag_buf.append(payload)
                    if fin and frag_buf:
                        joined = b"".join(frag_buf)
                        frag_buf = []
                        self._on_payload(joined.decode("utf-8", errors="replace"))
        finally:
            was_connected = self.connected
            self.connected = False
            self._sock = None
            try:
                sock.close()
            except OSError:
                pass
            if was_connected:
                # 曾连上又断开：这段窗口的事件服务端不会重放，
                # 由 logs 增量读补齐——这里只如实记录，不假装没丢过。
                self.last_error = self.last_error or "连接断开，重连中"

    # ---- 协议细节 ----
    def _handshake(self, sock: socket.socket) -> bytes:
        """HTTP Upgrade 握手；返回多读出来的字节（可能是首帧开头）。"""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET " + self._path + " HTTP/1.1\r\n"
            "Host: " + self._host + ":" + str(self._port) + "\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: " + key + "\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        resp = bytearray()
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("握手阶段对端关闭")
            resp += chunk
            if len(resp) > 16384:
                raise ConnectionError("握手响应异常过大")
        status = resp.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = status.split()
        if len(parts) < 2 or parts[1] != "101":
            raise ConnectionError("握手被拒：" + status[:120])
        return bytes(resp.split(b"\r\n\r\n", 1)[1])

    @staticmethod
    def _send_masked(sock: socket.socket, opcode: int, payload: bytes) -> None:
        """发一个客户端帧（RFC 6455：客户端→服务端必须掩码）。"""
        mask = os.urandom(4)
        header = bytes((0x80 | opcode, 0x80 | len(payload))) + mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        sock.sendall(header + masked)


# ============================================================ 事件来源（双源）


# 事件拆包唯一实现在 domain/events.py（阶段 5 去重）；这里保留旧名供本模块与
# report.py 的既有调用点使用，行为与 assist_rules.unwrap_events 完全一致。
from .domain.events import unwrap_events as _unwrap  # noqa: E402
from .infra.logtail import read_new_bytes  # noqa: E402


def _fingerprint(event: dict[str, Any]) -> tuple[Any, ...]:
    """事件指纹：把「WS 与 logs 重复投递的同一事件」去掉，同时尽量区分
    「同一毫秒的不同事件」。

    在 (session_id, timestamp_ms, type, inst_id) 基础上补首条 detail 的数值——
    同一事件的两种投递 JSON 完全一致（指纹必同），而同毫秒两次不同命中的
    伤害数值几乎必然不同。错误去重的后果只是少记一次命中，比漏事件轻得多。
    """
    inst = (event.get("entity_inst_id") or event.get("buff_inst_id")
            or event.get("skill_inst_id") or event.get("seq_id"))
    value: Any = None
    details = event.get("details")
    if isinstance(details, list) and details and isinstance(details[0], dict):
        value = details[0].get("abs_value", details[0].get("value"))
    if value is None:
        action = event.get("action")
        if isinstance(action, dict):
            dmg = action.get("damage_action")
            if isinstance(dmg, dict):
                inner = dmg.get("details")
                if (isinstance(inner, list) and inner
                        and isinstance(inner[0], dict)):
                    value = inner[0].get("abs_value", inner[0].get("value"))
    return (event.get("session_id"), event.get("timestamp_ms"),
            event.get("type"), inst, value)


class ZoneReader:
    """事件来源：持久 WS 订阅 + logs 增量尾读，双源合并去重。**只读**，只连本地回环。

    - WS：`_WsThread` 长连订阅（断线重连；服务端不重放，断口由 logs 补）；
    - logs：`<logs>/<日期>/session_*.ndjson` 按文件偏移增量读。**logs 是全量
      真值**（ZMDLogs 每个事件都落盘），所以每轮 poll 两边都读、指纹去重合并；
    - session 文件按 **mtime** 取最新几个（按路径字典序会漏掉正在写的 session——
      同一天目录里的 session UUID 不是时间序）。
    """

    def __init__(self, cfg: AssistConfig) -> None:
        self.cfg = cfg
        self.ws_url = cfg.ws_url or DEFAULT_WS_URL
        self._explicit_dir = str(cfg.log_dir or "").strip()
        self._explicit_path: Path | None = (
            Path(os.path.expandvars(self._explicit_dir)).expanduser()
            if self._explicit_dir else None)
        #: ZMDLogs 启动后对过来的目录（子进程刚拉起时可能还没建，出现即生效）
        self._noted_dir: Path | None = None
        #: 最近一次实际采用的目录（state() 展示用）
        self.log_dir: Path | None = None
        self._offsets: dict[str, int] = {}
        self._seen: set[tuple[Any, ...]] = set()
        self.source = "none"
        self.last_error = ""
        self.ws = _WsThread(self.ws_url, self._on_ws_payload)
        self._ws_buffer: list[dict[str, Any]] = []
        self._ws_lock = threading.Lock()

    # ---- WS 侧 ----
    def _on_ws_payload(self, text: str) -> None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        events = _unwrap(payload)
        if not events:
            return
        with self._ws_lock:
            self._ws_buffer.extend(events)
            if len(self._ws_buffer) > 100_000:  # poll 长期不被调用时的上限保护
                del self._ws_buffer[:-100_000]

    def _drain_ws(self) -> list[dict[str, Any]]:
        with self._ws_lock:
            if not self._ws_buffer:
                return []
            out = self._ws_buffer
            self._ws_buffer = []
            return out

    # ---- logs 侧 ----
    def _resolve_dir(self) -> Path | None:
        """解析 logs 目录：显式配置 > runner 对齐 > 常见位置（每次 poll 兜底重探）。

        显式/对齐的目录此刻可能还没被创建（ZMDLogs 刚拉起）——存在性检查
        每轮都做，出现即接上；两者都缺席时才用候选位置（要求真有 session 文件）。
        """
        if self._explicit_path is not None:
            if self._explicit_path.exists():
                self.log_dir = self._explicit_path
                return self._explicit_path
            return None
        if self._noted_dir is not None and self._noted_dir.exists():
            self.log_dir = self._noted_dir
            return self._noted_dir
        found = find_logs_dir("")
        if found is not None:
            self.log_dir = found
        return found

    def note_log_dir(self, path: str) -> bool:
        """ZMDLogs 启动后把它实际写的 logs 目录对过来（仅当没有显式配置时）。

        目录此刻可能还没被创建（子进程刚拉起）——存下即返回 True，
        `_resolve_dir` 每轮检查它是否出现。
        """
        if not path or self._explicit_path is not None:
            return False
        cand = Path(path)
        if self._noted_dir is not None and str(self._noted_dir) == str(cand):
            return False
        self._noted_dir = cand
        return True

    def _read_file_new(self, path: Path) -> list[dict[str, Any]]:
        """增量读一个 ndjson：只解析上次读过后新追加的完整行。

        增量与半行的处理在 infra/logtail.py（阶段 5 去重，与 stream 共用一份）；
        这里只负责「按路径记偏移」和「把行解析成事件」。
        """
        key = str(path)
        first = self._offsets.get(key, -1)
        if first < 0:
            # 陈旧抓包不回看（v0.2.2）：偏移直接落到文件尾，今后只读新追加的行。
            # 进行中/刚打完的战斗 mtime 是秒级的，回看照常——WS 断连补口的语义保住。
            # stat 异常必须就地吞掉：poll 已先排空 WS 缓冲，异常外抛会丢掉整轮 WS 事件。
            try:
                st = path.stat()
                if time.time() - st.st_mtime > LOG_FRESH_SECONDS:
                    first = st.st_size
            except OSError:
                pass
        data, new_offset = read_new_bytes(path, first, tail_bytes=LOG_TAIL_BYTES)
        self._offsets[key] = new_offset
        if not data:
            return []
        text = data.decode("utf-8", errors="replace")
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.extend(_unwrap(obj))
        return out

    def _drain_logs(self) -> list[dict[str, Any]]:
        root = self._resolve_dir()
        if root is None:
            return []
        try:
            files = [p for p in root.rglob("session_*.ndjson") if p.is_file()]
        except OSError:
            return []
        if not files:
            return []
        try:
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            files.sort(key=lambda p: str(p), reverse=True)
        files = files[:LOG_TRACK_FILES]
        out: list[dict[str, Any]] = []
        for path in files:
            out.extend(self._read_file_new(path))
        if len(self._offsets) > 64:  # 偏移表瘦身：不再跟踪的文件别攒着
            keep = {str(p) for p in files}
            self._offsets = {k: v for k, v in self._offsets.items() if k in keep}
        return out

    # ---- 合并 ----
    def poll(self) -> list[dict[str, Any]]:
        """取一批**新**事件（双源合并 + 指纹去重）。

        WS 断连期间 ZMDLogs 不重放，但每个事件都会落 ndjson ⇒ logs 增量读
        天然补齐断口；两源对同一事件的 JSON 完全同构，指纹去重即可合并。
        """
        self.ws.ensure_started()
        ws_events = self._drain_ws()
        log_events = self._drain_logs()
        if ws_events and log_events:
            self.source = "ws+logs"
        elif ws_events:
            self.source = "ws"
        elif log_events:
            self.source = "logs"
        if self.ws.last_error:
            self.last_error = self.ws.last_error
        fresh: list[dict[str, Any]] = []
        for event in ws_events + log_events:
            key = _fingerprint(event)
            if key in self._seen:
                continue
            self._seen.add(key)
            fresh.append(event)
        # 指纹集合不能无限增长（重放防护：见 _fingerprint 注释）
        if len(self._seen) > 200_000:
            self._seen.clear()
        return fresh

    def close(self) -> None:
        self.ws.stop()


# ============================================================ 缓冲与判定


def _is_battle_end(event: Any) -> bool:
    """战斗结束标记（ws-reverse-engineering.md §4.3）：`is_in_battle=false`。"""
    return (isinstance(event, dict)
            and event.get("type") == BATTLE_STATE_TYPE
            and not bool(event.get("is_in_battle", False)))


class AssistService:
    """后台陪玩/卡关辅助服务。

    由 router 在 command-loop 钩子里用 `asyncio.create_task` 启动（同 `_remind_loop`
    的模式；startup 短循环会连任务一起取消，v0.2.1 起改挂宿主长命令循环），
    shutdown 时 cancel。**不持有任何游戏输入能力**。
    """

    def __init__(
        self,
        cfg: AssistConfig,
        *,
        log: Callable[[str, str], None] | None = None,
        push: Callable[[str], Any] | None = None,
        scene_probe: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self.cfg = cfg
        self._log = log or (lambda level, msg: None)
        self._push = push
        #: 场景佐证采集（可选）：由 router 注入，其内部保证「仅游戏前台才采集」
        #: （不抢焦点，M4 条件提醒同款原则）；失败/非前台返回 None，绝不阻塞判定。
        self._scene_probe = scene_probe
        self.reader = ZoneReader(cfg)
        self.recent: list[AssistEvent] = []
        self.counters: dict[str, int] = {
            "polls": 0, "events": 0, "battles": 0, "stuck": 0, "pushed": 0, "errors": 0,
        }
        self._counter = FailureCounter(
            threshold=cfg.consecutive_fail_threshold, scope="global")
        self._fingerprints: set[str] = set()
        self._running = False
        # 跨轮缓冲：poll 边界不该把战斗切碎（见模块 docstring「事件来源与送达率」）
        self._pending: list[dict[str, Any]] = []
        self._pending_since = 0.0

    # ---- 单批处理（纯逻辑，便于单测）----
    def process(self, events: list[dict[str, Any]],
                flush: bool = False) -> list[AssistEvent]:
        """把新到事件并入缓冲，攒到「战斗结束标记」再整体切分判定。

        为什么要缓冲：poll 边界会把同一场战斗切成碎片——碎片让
        `started_at_ms`/`event_count` 全错、`FailureCounter` 的连续计数被
        反复冲刷。攒到 `is_in_battle=false` 再切分，一场战斗就是完整的一份；
        `flush=True`（`ef_assist check` 手动触发）或兜底阈值到了才处理不完整缓冲。
        """
        if events:
            self.counters["events"] += len(events)
            if not self._pending:
                self._pending_since = time.monotonic()
            self._pending.extend(events)
        if not self._pending:
            return []

        cut = 0
        for i, event in enumerate(self._pending):
            if _is_battle_end(event):
                cut = i + 1
        forced = (
            len(self._pending) >= PENDING_FLUSH_EVENTS
            or (self._pending_since > 0
                and time.monotonic() - self._pending_since > PENDING_FLUSH_SECONDS)
        )
        if cut == 0 and not (flush or forced):
            return []
        if cut:
            batch = self._pending[:cut]
            del self._pending[:cut]
        else:
            batch = list(self._pending)
            self._pending.clear()
        self._pending_since = time.monotonic() if self._pending else 0.0
        if not batch:
            return []

        produced: list[AssistEvent] = []
        # 场景佐证：一次 process 只采一次，同一时刻的画面复用到本轮全部建议。
        # 采集本身"仅游戏前台才动手"（router 注入的 probe 把关，M4 条件提醒同款原则）；
        # 任何失败都只是佐证缺席，不影响判定。
        scene_ev: dict[str, Any] | None = None
        if self._scene_probe is not None:
            try:
                scene_ev = self._scene_probe()
            except Exception:  # noqa: BLE001 - 佐证失败不阻塞判定
                scene_ev = None
        rules_cfg = RulesConfig(expected_party_size=4)
        stuck_cfg = StuckConfig(
            consecutive_fail_threshold=self.cfg.consecutive_fail_threshold)
        self._counter.threshold = self.cfg.consecutive_fail_threshold
        try:
            battles = split_battles(batch)
        except Exception as exc:  # noqa: BLE001
            self.counters["errors"] += 1
            self._log("warning", f"战斗切分失败：{exc}")
            return []

        # 按时间顺序处理：连续失败计数依赖先后次序
        for battle in sorted(battles, key=lambda b: (b.started_at_ms, str(b.session_id or ""))):
            fingerprint = self._counter.fingerprint(battle)
            if fingerprint in self._fingerprints:
                continue  # 重放（WS/logs 之外的整场重复）不重复计数
            self._fingerprints.add(fingerprint)
            self.counters["battles"] += 1

            # 先登记失败（驱动连续计数），再判定卡关——与 labeler 的 scan_battles 同序
            failed = battle_failed(battle, stuck_cfg)
            count = self._counter.observe(battle, failed)
            signal = detect_stuck(battle, count, stuck_cfg)
            if not signal.stuck:
                continue
            self.counters["stuck"] += 1

            result = classify_events(battle, rules_cfg)
            session = str(battle.session_id or "unknown")
            macro = MACRO_BY_LABEL.get(result.label, MACRO_BY_LABEL["unknown"])
            macro += "\n" + _stuck_suffix(signal.signal, count,
                                          self.cfg.consecutive_fail_threshold)

            fine = None
            suppressed = None
            if self.cfg.assist_mode != "精细建议":
                suppressed = "当前是「宏观建议」模式"
            elif result.label == "unknown":
                suppressed = "没有达到可归因的失败原因"
            elif result.confidence < self.cfg.confidence_threshold:
                suppressed = (f"归因置信度 {result.confidence:.2f} "
                              f"低于门槛 {self.cfg.confidence_threshold:.2f}")
            else:
                fine = _fine_text(result)

            produced.append(AssistEvent(
                ts=time.time(),
                session_id=session,
                signal=signal.signal,
                label=result.label,
                confidence=result.confidence,
                macro=macro,
                fine=fine,
                fine_suppressed_reason=suppressed,
                consecutive_count=count,
                scene_evidence=scene_ev,
            ))

        if len(self._fingerprints) > 50_000:
            self._fingerprints.clear()
        self.recent.extend(produced)
        if len(self.recent) > 50:
            self.recent = self.recent[-50:]
        return produced

    # ---- 后台循环 ----
    async def run(self) -> None:
        """后台轮询循环。循环体全体包 try/except：单次异常不得终止循环。"""
        self._running = True
        if not self.cfg.enabled:
            self._log("info", "卡关辅助未启用（assist_enabled=false），待命中")
            return
        self._log("info", f"卡关辅助待命（持久订阅 {self.reader.ws_url}，"
                          f"poll={self.cfg.poll_seconds}s "
                          f"mode={self.cfg.assist_mode} conf={self.cfg.confidence_threshold} "
                          f"consec={self.cfg.consecutive_fail_threshold})")
        while self._running:
            try:
                self.counters["polls"] += 1
                events = await asyncio.to_thread(self.reader.poll)
                if events:
                    for item in self.process(events):
                        await self._emit(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.counters["errors"] += 1
                self._log("warning", f"卡关辅助轮询异常（继续）：{type(exc).__name__}: {exc}")
            await asyncio.sleep(self.cfg.poll_seconds)

    async def _emit(self, item: AssistEvent) -> None:
        """推送一条建议。推送失败不影响判定与记录。"""
        text = item.text()
        self._log("info", f"[卡关辅助] {item.signal} → {item.label} "
                          f"(conf={item.confidence:.2f}, 连续{item.consecutive_count})\n{text}")
        if self._push is None:
            return
        try:
            result = self._push(text)
            if asyncio.iscoroutine(result):
                await result
            self.counters["pushed"] += 1
        except Exception as exc:  # noqa: BLE001
            self.counters["errors"] += 1
            self._log("warning", f"推送失败（不影响判定）：{exc}")

    def stop(self) -> None:
        self._running = False
        self.reader.close()

    def note_log_dir(self, path: str) -> bool:
        """ZMDLogs 启动后对齐它的 logs 目录（转发给 reader，仅当还没有时）。"""
        return self.reader.note_log_dir(path)

    def state(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "enabled": self.cfg.enabled,
            "source": self.reader.source,
            "last_error": self.reader.last_error,
            "ws": {
                "connected": self.reader.ws.connected,
                "sessions": self.reader.ws.sessions,
            },
            "pending_events": len(self._pending),
            "config": {
                "assist_mode": self.cfg.assist_mode,
                "confidence_threshold": self.cfg.confidence_threshold,
                "consecutive_fail_threshold": self.cfg.consecutive_fail_threshold,
            },
            "counters": dict(self.counters),
            "recent": [e.as_dict() for e in self.recent[-10:]],
        }

    def pending_snapshot(self) -> list[dict[str, Any]]:
        """只读拷贝当前缓冲，供 advice 空参回落；不消耗事件，后台循环照常切分。

        浅拷贝共享事件 dict：调用方只读（判定纯路径不改写输入）。
        """
        return list(self._pending)


# ---- 判定辅助（卡关三信号与归因均已移入 assist_rules，与 labeler 同构）----

def _stuck_suffix(signal: str, count: int, threshold: int) -> str:
    if signal == "settlement_failure":
        return f"已经连续 {count} 把结算失败，建议先停下来调整，别硬冲。"
    if signal == "party_wipe":
        return f"连续 {count} 把全员阵亡，队伍承压过大。"
    return f"已经连续 {count} 把没过（门槛 {threshold}），建议换思路或先提升练度。"


def _fine_text(result: Any) -> str | None:
    """精细建议：用归因证据里的具体数字拼可执行动作。"""
    metrics = getattr(result, "metrics", None) or {}
    label = result.label
    lines: list[str] = []
    duration_s = float(metrics.get("duration_ms") or 0) / 1000.0

    if label == "configuration_error":
        size = metrics.get("party_size") or 0
        expected = metrics.get("expected_party_size") or 4
        if size and size < expected:
            lines.append(f"队伍只有 {size} 人（期望 {expected} 人）：先把编队补满。")
    elif label == "survivability_insufficient":
        first_ms = metrics.get("first_death_ms")
        if first_ms is not None:
            lines.append(f"首次减员发生在开战后 {float(first_ms)/1000:.0f} 秒，属于过早掉人："
                         "先看该队员的抗性/血量配置，或提前开减伤。")
        deaths = metrics.get("deaths") or 0
        size = metrics.get("party_size") or 0
        if deaths and size:
            lines.append(f"本场阵亡 {deaths}/{size} 人，队伍续航不足：考虑带一个奶位。")
    elif label == "skill_rotation_error":
        rate = metrics.get("skill_per_minute")
        if rate is not None:
            lines.append(f"技能释放频率只有 {float(rate):.1f} 次/分钟"
                         f"（{metrics.get('skill_count')} 次 / {duration_s:.0f} 秒），"
                         "明显偏低：注意及时切人放技能。")
        gap = float(metrics.get("max_skill_gap_ms") or 0) / 1000.0
        if gap > 0:
            lines.append(f"最长技能空窗达 {gap:.0f} 秒，占战斗 "
                         f"{float(metrics.get('max_skill_gap_ratio') or 0):.0%}："
                         "中段有长时间没放技能，检查循环顺序。")
    elif label == "dps_insufficient":
        silent = metrics.get("silent_members") or 0
        size = metrics.get("party_size") or 0
        if silent:
            lines.append(f"检查输出位是否真的在打：本场有 {silent}/{size} 名队员全程零伤害。")
        if metrics.get("enemy_deaths") == 0 and duration_s > 0:
            lines.append(f"战斗持续 {duration_s:.0f} 秒仍未击杀敌方"
                         f"（总伤害 {float(metrics.get('total_damage') or 0):.0f}）："
                         "考虑换更高倍率的输出位或先提升练度。")

    return "\n".join(lines) if lines else None
