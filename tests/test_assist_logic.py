#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""assist.py 纯逻辑自检：双源事件送达 / 指纹去重 / 跨轮缓冲 / WS 帧解析。

跑法：python tests/test_assist_logic.py   （零第三方依赖；WS 用例起本地回环假服务端）

背景（2026-10-12 修整，依据 v7 选型报告 §S9 与 verify-hw-tier-staged-plan-correction）：
旧 ZoneReader 每 poll 周期只连 WS 听 2 秒（周期 ~7 s ⇒ 送达率 ~29%，且服务端
不重放），每轮还独立切分战斗 ⇒ 碎片化。本文件守护三条新不变量：

1. **持久订阅**：WS 长连，事件持续入缓冲（含 ping→pong 保活）；
2. **logs 增量尾读**：按文件偏移只解析新字节，与 WS 缓冲指纹去重合并；
3. **跨轮缓冲**：攒到 `is_in_battle=false` 才切分——同一场战斗跨 poll 只算一场。
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maaend.assist as assist  # noqa: E402
from maaend.assist import (  # noqa: E402
    AssistConfig,
    AssistService,
    ZoneReader,
    _fingerprint,
    _FrameReader,
    _is_battle_end,
    _WsThread,
)

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), label, detail))
    line = "[%s] %s" % ("PASS" if ok else "FAIL", label)
    if detail:
        line += "  -> " + detail
    print(line, flush=True)


def _wait_for(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- #
# 测试辅助
# --------------------------------------------------------------------------- #

class _FakeSock:
    """按小块吐出预置字节的假 socket（recv 空了返回 b''，与真 socket 一致）。"""

    def __init__(self, data: bytes, chunk: int = 4) -> None:
        self._data = data
        self._chunk = chunk
        self.sent = bytearray()

    def recv(self, n: int) -> bytes:
        if not self._data:
            return b""
        size = max(1, min(n, self._chunk, len(self._data)))
        out = self._data[:size]
        self._data = self._data[size:]
        return out

    def sendall(self, b) -> None:
        self.sent += b

    def settimeout(self, t) -> None:
        pass

    def close(self) -> None:
        pass


def _srv_text_frame(payload: bytes) -> bytes:
    """服务端→客户端文本帧（RFC 6455：服务端不掩码）。"""
    n = len(payload)
    if n < 126:
        return bytes((0x81, n)) + payload
    if n < 65536:
        return bytes((0x81, 126)) + n.to_bytes(2, "big") + payload
    return bytes((0x81, 127)) + n.to_bytes(8, "big") + payload


class _MiniWsServer:
    """最小 WS 服务端：握手 → 推 hello → 持续收客户端帧（测试 pong 用）。"""

    def __init__(self) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self._srv.settimeout(5.0)
        self.port = self._srv.getsockname()[1]
        self.conn: socket.socket | None = None
        self.received = bytearray()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        self.conn = conn
        conn.settimeout(5.0)
        try:
            req = bytearray()
            while b"\r\n\r\n" not in req:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                req += chunk
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                         b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: test\r\n\r\n")
            # 首帧 hello：客户端必须忽略（ws-reverse-engineering.md §7.7）
            conn.sendall(_srv_text_frame(
                json.dumps({"type": "hello", "schema_version": 2}).encode()))
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                self.received += chunk
        except OSError:
            pass

    def send_text(self, text: str) -> None:
        if self.conn is not None:
            try:
                self.conn.sendall(_srv_text_frame(text.encode()))
            except OSError:
                pass

    def send_ping(self) -> None:
        if self.conn is not None:
            try:
                self.conn.sendall(bytes((0x89, 0)))
            except OSError:
                pass

    def close(self) -> None:
        for s in (self.conn, self._srv):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def _dead_ws_url() -> str:
    """保证没有服务端的 WS 地址（端口 1 本机几乎必关）。"""
    return "ws://127.0.0.1:1/ws"


def _evt(kind: str, ts: int, session: str = "s1", **extra) -> dict:
    return dict({"type": kind, "session_id": session, "timestamp_ms": ts}, **extra)


def _failed_battle_events(session: str, start_ms: int) -> list[dict]:
    """一场「全灭速败」战斗的完整事件序列（含结束标记）。

    顺序对齐真实流：squad_update 先于 is_in_battle=true（ws-reverse-engineering
    §6：队伍在场景加载时就建立）——若反着写，split_battles 的「换队=结算上一场」
    启发式会把战斗一切为二。
    """
    evs = [
        _evt("squad_update", start_ms, session, members=[
            {"battle_inst_id": i, "display_name": f"op{i}", "squad_index": i}
            for i in (1, 2, 3, 4)]),
        _evt("BattleOpModifyBattleState", start_ms + 10, session, is_in_battle=True),
    ]
    for k in range(3):
        evs.append(_evt("damage", start_ms + 1000 + k * 500, session,
                        attacker={"battle_inst_id": 1},
                        target={"battle_inst_id": 99},
                        details=[{"target_battle_inst_id": 99, "abs_value": 100.0,
                                  "value": -100.0, "cur_hp": None}]))
    for i in (1, 2, 3, 4):
        evs.append(_evt("BattleOpEntityDie", start_ms + 3000, session,
                        entity_inst_id=i))
    evs.append(_evt("BattleOpModifyBattleState", start_ms + 3200, session,
                    is_in_battle=False))
    return evs


# --------------------------------------------------------------------------- #
# 1. 指纹与结束标记（纯函数）
# --------------------------------------------------------------------------- #

def test_fingerprint_and_marker() -> None:
    a = _evt("damage", 100, details=[{"abs_value": 50.0}])
    b = _evt("damage", 100, details=[{"abs_value": 50.0}])
    check(_fingerprint(a) == _fingerprint(b),
          "指纹：同一事件的两种投递（WS/logs）指纹相同")
    c = _evt("damage", 100, details=[{"abs_value": 60.0}])
    check(_fingerprint(a) != _fingerprint(c),
          "指纹：同毫秒不同数值的事件可区分")
    # BattleOpTriggerAction 形态的嵌套 details 也要参与指纹
    d = _evt("BattleOpTriggerAction", 200, action={
        "action_type": "BattleActionDamage",
        "damage_action": {"details": [{"abs_value": 7.0}]}})
    e = _evt("BattleOpTriggerAction", 200, action={
        "action_type": "BattleActionDamage",
        "damage_action": {"details": [{"abs_value": 7.0}]}})
    check(_fingerprint(d) == _fingerprint(e),
          "指纹：TriggerAction 嵌套 details 同构判定")

    check(_is_battle_end(_evt("BattleOpModifyBattleState", 1, is_in_battle=False)),
          "结束标记：is_in_battle=false")
    check(not _is_battle_end(_evt("BattleOpModifyBattleState", 1, is_in_battle=True)),
          "结束标记：is_in_battle=true 不算")
    check(not _is_battle_end(_evt("damage", 1)),
          "结束标记：其他事件不算")


# --------------------------------------------------------------------------- #
# 2. logs 增量尾读（临时目录，不碰真实 ZMDLogs）
# --------------------------------------------------------------------------- #

def test_logs_incremental() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        day = Path(tmp) / "20261012"
        day.mkdir()
        f = day / "session_test1.ndjson"
        e1 = [_evt("damage", 100, "sx"), _evt("damage", 200, "sx")]
        f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in e1) + "\n",
                     encoding="utf-8")
        reader = ZoneReader(AssistConfig(ws_url=_dead_ws_url(),
                                         log_dir=str(Path(tmp))))
        try:
            got1 = reader.poll()
            check(len(got1) == 2 and reader.source == "logs",
                  "logs 首读：两事件全部送达", f"got={len(got1)} src={reader.source}")
            # 追加新事件：只应读到新增的
            e2 = [_evt("damage", 300, "sx"), _evt("damage", 400, "sx")]
            with f.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(json.dumps(x) for x in e2) + "\n")
            got2 = reader.poll()
            check(len(got2) == 2 and all(x["timestamp_ms"] in (300, 400) for x in got2),
                  "logs 增量：第二轮只读到新增事件", f"got={[x['timestamp_ms'] for x in got2]}")
            # 半行：写半截 JSON（无换行）→ 本轮不读，补全后下轮读
            with f.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_evt("damage", 500, "sx"))[:20])
            got3 = reader.poll()
            check(got3 == [], "logs 半行回退：未完成的行不入流", f"got={len(got3)}")
            with f.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_evt("damage", 500, "sx"))[20:] + "\n")
            got4 = reader.poll()
            check(len(got4) == 1 and got4[0]["timestamp_ms"] == 500,
                  "logs 半行补全：下一轮完整读出")
        finally:
            reader.close()


def test_logs_truncation_and_pick_latest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        day = Path(tmp) / "20261012"
        day.mkdir()
        old = day / "session_old.ndjson"
        new = day / "session_new.ndjson"
        old.write_text(json.dumps(_evt("damage", 10, "s-old")) + "\n", encoding="utf-8")
        new.write_text(json.dumps(_evt("damage", 20, "s-new")) + "\n", encoding="utf-8")
        reader = ZoneReader(AssistConfig(ws_url=_dead_ws_url(),
                                         log_dir=str(Path(tmp))))
        try:
            got = reader.poll()
            check(len(got) == 2,
                  "logs 多文件：同目录多个 session 都在跟踪范围", f"got={len(got)}")
            # 截断（size < offset）→ 偏移归零重读；write_text 整体替换后旧内容不再存在
            old.write_text("", encoding="utf-8")
            got_reset = reader.poll()
            check(got_reset == [], "logs 截断：空文件一轮完成偏移归零")
            old.write_text(json.dumps(_evt("damage", 11, "s-old")) + "\n", encoding="utf-8")
            got2 = reader.poll()
            check(len(got2) == 1 and got2[0]["timestamp_ms"] == 11,
                  "logs 截断重读：从头读出新内容", f"got={[x['timestamp_ms'] for x in got2]}")
        finally:
            reader.close()


def test_find_logs_dir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / "empty"
        empty.mkdir()
        found = assist.find_logs_dir(str(empty))
        check(found is not None and str(found) == str(empty),
              "find_logs_dir：显式目录存在即接受（哪怕还没 session）")
        # 候选位置要求真有 session 文件（打到临时目录，避免依赖本机 ZMDLogs）
        cand_root = Path(tmp) / "cand" / "logs"
        cand_root.mkdir(parents=True)
        (cand_root / "20261012").mkdir()
        (cand_root / "20261012" / "session_x.ndjson").write_text("{}\n", encoding="utf-8")
        orig = assist.LOG_DIR_CANDIDATES
        try:
            # 显式路径不存在时才走候选——把候选指到临时目录，避免被本机
            # 真实 ZMDLogs 目录（%LOCALAPPDATA%/ZMDLogs/logs）干扰判定
            assist.LOG_DIR_CANDIDATES = (str(cand_root),)
            check(assist.find_logs_dir(str(Path(tmp) / "nope")) == cand_root,
                  "find_logs_dir：显式路径不存在 → 落到候选")
            hit = assist.find_logs_dir("")
            check(hit is not None and str(hit) == str(cand_root),
                  "find_logs_dir：候选位置有 session 文件 → 命中")
            bare = Path(tmp) / "bare"
            bare.mkdir()
            assist.LOG_DIR_CANDIDATES = (str(bare),)
            check(assist.find_logs_dir("") is None,
                  "find_logs_dir：空候选目录不算可用（不猜、不假装）")
            assist.LOG_DIR_CANDIDATES = ()
            check(assist.find_logs_dir(str(Path(tmp) / "nope")) is None,
                  "find_logs_dir：显式不存在且无候选 → None")
        finally:
            assist.LOG_DIR_CANDIDATES = orig


# --------------------------------------------------------------------------- #
# 3. 双源合并去重（WS 缓冲 + logs 同事件）
# --------------------------------------------------------------------------- #

def test_dual_source_dedup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        day = Path(tmp) / "20261012"
        day.mkdir()
        f = day / "session_dedup.ndjson"
        shared = [_evt("damage", 100, "sd", details=[{"abs_value": 5.0}]),
                  _evt("damage", 101, "sd", details=[{"abs_value": 6.0}])]
        f.write_text("\n".join(json.dumps(x) for x in shared) + "\n", encoding="utf-8")
        reader = ZoneReader(AssistConfig(ws_url=_dead_ws_url(),
                                         log_dir=str(Path(tmp))))
        try:
            # 模拟 WS 先收到同一批事件（直接喂缓冲，等价于线程回调路径）
            reader._on_ws_payload(json.dumps(
                {"type": "event_batch", "session_id": "sd", "events": shared}))
            got = reader.poll()
            kinds = [x["timestamp_ms"] for x in got]
            check(len(got) == 2, "双源合并：WS 与 logs 的同一批事件只入流一次",
                  f"got={kinds} src={reader.source}")
            check(reader.source in ("ws+logs", "logs"),
                  "双源合并：来源标记如实", reader.source)
        finally:
            reader.close()


# --------------------------------------------------------------------------- #
# 4. WS 帧解析（_FrameReader / _WsThread 握手）
# --------------------------------------------------------------------------- #

def test_frame_reader() -> None:
    payload_small = b"hello"
    payload_big = b"x" * 300
    mask = bytes((1, 2, 3, 4))
    masked_payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload_small))
    data = (
        _srv_text_frame(payload_small)                 # 小帧
        + _srv_text_frame(payload_big)                 # 126 扩展长度
        + bytes((0x81, 0x80 | len(payload_small))) + mask + masked_payload  # 防御：带掩码
        + bytes((0x89, 0))                             # ping（空载荷）
        + bytes((0x8A, 0))                             # pong（忽略）
        + bytes((0x01, 2)) + b"ab"                     # 分片：首片 fin=0（长 2）
        + bytes((0x00, 2)) + b"cd"                     # 续片
        + bytes((0x80, 2)) + b"ef"                     # 终片 fin=1
    )
    r = _FrameReader(_FakeSock(data))
    fin, op, p = r.read_frame()
    check(fin and op == 0x1 and p == b"hello", "帧解析：小文本帧")
    fin, op, p = r.read_frame()
    check(fin and op == 0x1 and p == payload_big, "帧解析：126 扩展长度")
    fin, op, p = r.read_frame()
    check(fin and op == 0x1 and p == payload_small, "帧解析：掩码帧（防御路径）")
    fin, op, p = r.read_frame()
    check(op == 0x9 and p == b"", "帧解析：ping 帧")
    fin, op, p = r.read_frame()
    check(op == 0xA, "帧解析：pong 帧")
    fin, op, p = r.read_frame()
    check(not fin and op == 0x1 and p == b"ab",
          "帧解析：分片首帧（fin=0、opcode=0x1 文本）")
    fin, op, p = r.read_frame()
    check(not fin and op == 0x0 and p == b"cd", "帧解析：分片续帧")
    fin, op, p = r.read_frame()
    check(fin and p == b"ef", "帧解析：分片终帧")
    # 读尽后：recv 返回 b'' → ConnectionError
    try:
        r.read_frame()
        check(False, "帧解析：对端关闭抛 ConnectionError")
    except ConnectionError:
        check(True, "帧解析：对端关闭抛 ConnectionError")


def test_ws_handshake() -> None:
    t = _WsThread("ws://127.0.0.1:1/ws", lambda s: None)
    check((t._host, t._port, t._path) == ("127.0.0.1", 1, "/ws"),
          "URL 解析：host/port/path")
    # 成功握手：多读出的字节要交回帧读取器（chunk=13 与头部 42 字节不对齐，
    # 必然把首帧字节一起吞进 resp——leftover 路径的严格断言）
    leftover = b"\x81\x02hi"
    resp = b"HTTP/1.1 101 Switching Protocols\r\nX: y\r\n\r\n" + leftover
    sock = _FakeSock(resp, chunk=13)
    got = t._handshake(sock)
    check(got == leftover, "握手成功：多读出的首帧字节交还帧读取器", repr(got))
    # 真正的不变量：无论 chunk 边界落在哪里，leftover + socket 剩余数据
    # 必须无缝拼出完整帧（对齐时 leftover 可能为空，剩余字节留在内核缓冲）
    for chunk in (1, 7, 13, len(resp)):
        sock2 = _FakeSock(resp, chunk=chunk)
        got2 = t._handshake(sock2)
        r = _FrameReader(sock2, got2)
        fin, op, p = r.read_frame()
        check(fin and op == 0x1 and p == b"hi",
              f"握手边界（chunk={chunk}）：多读字节与剩余数据无缝拼接",
              f"leftover={got2!r}")
    # 非 101 → 拒绝
    try:
        t._handshake(_FakeSock(b"HTTP/1.1 404 Not Found\r\n\r\n"))
        check(False, "握手失败：非 101 抛 ConnectionError")
    except ConnectionError:
        check(True, "握手失败：非 101 抛 ConnectionError")
    # 客户端帧必须掩码（pong 载荷空）；结构由回环用例验证
    t._send_masked(_FakeSock(b""), 0xA, b"")


# --------------------------------------------------------------------------- #
# 5. 端到端：真回环 WS（持久订阅 + hello 忽略 + ping→pong）
# --------------------------------------------------------------------------- #

def test_ws_end_to_end() -> None:
    server = _MiniWsServer()
    server.start()
    with tempfile.TemporaryDirectory() as tmp:
        # log_dir 指到空临时目录：隔离本机真实 ZMDLogs 日志，避免真实事件混入
        reader = ZoneReader(AssistConfig(
            ws_url=f"ws://127.0.0.1:{server.port}/ws", log_dir=str(tmp)))
        try:
            reader.poll()  # 首个 poll 拉起订阅线程
            # 同步点：等客户端**握手完成**（sessions>=1），不能只等 TCP accept——
            # 否则服务端可能在发出 101 之前就把 event_batch 推出去，破坏帧序
            check(_wait_for(lambda: reader.ws.sessions >= 1),
                  "回环：客户端完成 TCP+WS 握手",
                  f"last_error={reader.ws.last_error}")
            events = [_evt("damage", 100, "se", details=[{"abs_value": 1.0}]),
                      _evt("BattleOpEntityDie", 105, "se", entity_inst_id=9)]
            server.send_text(json.dumps(
                {"type": "event_batch", "session_id": "se", "events": events}))
            got: list = []
            _wait_for(lambda: (got.extend(reader.poll()), len(got) >= 2)[1])
            check(len(got) == 2 and got[0]["type"] == "damage",
                  "回环：event_batch 事件送达（hello 未混入）", f"got={len(got)}")
            check(reader.source == "ws" and reader.ws.sessions >= 1,
                  "回环：来源=ws 且会话计数 ≥1",
                  f"src={reader.source} sessions={reader.ws.sessions}")
            # 保活：服务端 ping → 客户端必须回 pong（掩码帧，首字节 0x8A）
            server.send_ping()
            check(_wait_for(lambda: len(server.received) >= 6
                            and server.received[0] == 0x8A),
                  "回环：ping→pong 保活（客户端帧已掩码）")
        finally:
            reader.close()
            server.close()


# --------------------------------------------------------------------------- #
# 6. 跨轮缓冲：同一场战斗跨 poll 只算一场（碎片化修复的核心断言）
# --------------------------------------------------------------------------- #

def test_battle_buffering() -> None:
    svc = AssistService(AssistConfig(), log=lambda lv, msg: None)

    # 6.1 没有结束标记 → 不切分（旧实现会立刻切成碎片）
    evs = _failed_battle_events("buf", 1000)
    half1, half2 = evs[:5], evs[5:]
    out = svc.process(half1)
    check(out == [] and svc.counters["battles"] == 0,
          "缓冲：无结束标记不切分", f"battles={svc.counters['battles']}")
    out = svc.process(half2)  # 含 is_in_battle=false → 触发切分
    check(svc.counters["battles"] == 1,
          "缓冲：结束标记到达后整场切分（跨 poll 只算一场）",
          f"battles={svc.counters['battles']}")
    check(len(out) == 1 and out[0].session_id == "buf",
          "缓冲：失败战斗产出一条建议")
    check(out[0].consecutive_count == 1, "缓冲：连续失败计数=1（不因碎片重复）")

    # 6.2 同一场战斗重复喂入（模拟 WS/logs 重放）→ 战斗级指纹去重
    again = svc.process(_failed_battle_events("buf", 1000))
    check(again == [] and svc.counters["battles"] == 1,
          "缓冲：同一场战斗重复输入不重复计数")

    # 6.3 flush=True：手动检查不等结束标记
    svc2 = AssistService(AssistConfig(), log=lambda lv, msg: None)
    partial = _failed_battle_events("bf", 2000)[:6]
    out2 = svc2.process(partial)
    check(out2 == [], "flush=False：不完整战斗默认不判定")
    svc2.process([], flush=True)  # 只为触发 flush 副作用，返回值不在断言里用
    check(svc2.counters["battles"] >= 1,
          "flush=True：强制切分当前缓冲", f"battles={svc2.counters['battles']}")

    # 6.4 兜底阀：缓冲超过上限强制冲刷（不等结束标记）
    orig_limit = assist.PENDING_FLUSH_EVENTS
    try:
        assist.PENDING_FLUSH_EVENTS = 3
        svc3 = AssistService(AssistConfig(), log=lambda lv, msg: None)
        svc3.process([_evt("damage", k, "bv", details=[{"abs_value": 1.0}])
                      for k in (1, 2, 3)])
        check(svc3.counters["battles"] >= 1,
              "兜底阀：达到事件上限强制冲刷",
              f"battles={svc3.counters['battles']}")
    finally:
        assist.PENDING_FLUSH_EVENTS = orig_limit


def test_consecutive_failures() -> None:
    """三场完整失败战斗 → 连续计数递增，第 3 场给出连续失败信号。"""
    svc = AssistService(AssistConfig(), log=lambda lv, msg: None)
    counts = []
    for i in range(3):
        out = svc.process(_failed_battle_events("cf", 1000 + i * 10_000))
        counts.append(out[0].consecutive_count if out else 0)
    check(counts == [1, 2, 3], "连续失败计数按完整战斗递增", f"counts={counts}")


# --------------------------------------------------------------------------- #
# 7. state() 如实汇报新状态
# --------------------------------------------------------------------------- #

def test_state_report() -> None:
    svc = AssistService(AssistConfig(), log=lambda lv, msg: None)
    st = svc.state()
    check("ws" in st and "connected" in st["ws"] and "pending_events" in st,
          "state：暴露 ws.connected 与 pending_events",
          f"keys={sorted(k for k in st if k != 'recent')}")


def test_scene_probe_evidence() -> None:
    """场景佐证：注入 probe → 建议带 scene_evidence；probe 异常 → 不阻塞（佐证为 None）。"""
    calls = {"n": 0}

    def _probe():
        calls["n"] += 1
        return {"class": "战斗", "confidence": 0.7, "is_ui": False, "source": "test"}

    svc = AssistService(AssistConfig(), log=lambda lv, msg: None, scene_probe=_probe)
    out = svc.process(_failed_battle_events("sp", 3000))
    ok1 = (len(out) == 1 and out[0].scene_evidence is not None
           and out[0].scene_evidence.get("class") == "战斗")
    check(ok1, "场景佐证：建议携带 scene_evidence",
          f"got={out[0].scene_evidence if out else None}")
    check(calls["n"] == 1, "场景佐证：一次 process 只采一次", f"n={calls['n']}")
    check(out and out[0].as_dict().get("scene_evidence") is not None,
          "场景佐证：as_dict 含 scene_evidence 字段")

    def _boom():
        raise RuntimeError("no scene")

    svc2 = AssistService(AssistConfig(), log=lambda lv, msg: None, scene_probe=_boom)
    out2 = svc2.process(_failed_battle_events("sp2", 4000))
    check(len(out2) == 1 and out2[0].scene_evidence is None,
          "场景佐证：probe 异常不阻塞判定（佐证为 None）")


def main() -> int:
    tests = [
        test_fingerprint_and_marker,
        test_logs_incremental,
        test_logs_truncation_and_pick_latest,
        test_find_logs_dir,
        test_dual_source_dedup,
        test_frame_reader,
        test_ws_handshake,
        test_ws_end_to_end,
        test_battle_buffering,
        test_consecutive_failures,
        test_state_report,
        test_scene_probe_evidence,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - 单测异常按 FAIL 记录
            import traceback
            check(False, f"{fn.__name__} 异常", f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
    failed = sum(1 for r in RESULTS if not r[0])
    print()
    print("共 %d 项，失败 %d 项" % (len(RESULTS), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
