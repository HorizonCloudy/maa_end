"""maa_end 数据流 —— 后台采样 + 环形缓冲 + 游标读出。

三类数据流(YUI 经 maa_stream_read 随时直读):
- tasks:  maafw.log 增量解析出的任务生命周期事件 + 节点推进摘要
- window: 游戏窗口在线状态(hwnd/尺寸/标题)
- yolo:   定时窗口截图 + YOLO 检测结果(标签/置信度/框)

实现要点:
- 单 daemon 线程轮询(warthunder 范式);所有阻塞调用都在该线程,不碰事件循环;
- log tailer 记文件偏移增量读,不重复解析;轮转(rfind 到 .bak)按文件缩短重置;
- StreamStore 线程安全(单锁),全类共用自增 id,支持 since 游标拉增量;
- YOLO 懒加载,模型缺失/窗口离线时如实写入 error 条目而非中断流。
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .infra.logtail import read_new_bytes

#: 任务级事件行: msg=Tasker.Task.Succeeded] [details={"entry":"X"...}
_TASK_RE = re.compile(
    r"\[([\d:\. -]+)\].*msg=Tasker\.Task\.(Starting|Succeeded|Failed)"
    r'.*?"entry":"([A-Za-z0-9_]+)"'
)
#: 节点推进行(cur_node_)
_NODE_RE = re.compile(r"cur_node_=([A-Za-z0-9_]+)")

CATEGORIES = ("tasks", "window", "yolo")


class StreamStore:
    """线程安全环形缓冲;entries: {id, ts, category, kind, data}。"""

    def __init__(self, max_entries: int = 300):
        self._lock = threading.Lock()
        self._buffers: dict[str, deque] = {c: deque(maxlen=max_entries) for c in CATEGORIES}
        self._next_id = 1

    def append(self, category: str, kind: str, data: dict[str, Any]) -> int:
        with self._lock:
            entry_id = self._next_id
            self._next_id += 1
            self._buffers[category].append(
                {"id": entry_id, "ts": round(time.time(), 3), "category": category,
                 "kind": kind, "data": data}
            )
            return entry_id

    def read(self, category: str = "", since: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            cats = [category] if category in self._buffers else list(self._buffers)
            out: list[dict[str, Any]] = []
            for c in cats:
                out.extend(e for e in self._buffers[c] if e["id"] > since)
            out.sort(key=lambda e: e["id"])
            return out[-limit:]

    def latest_id(self) -> int:
        with self._lock:
            return self._next_id - 1


class _LogTailer:
    """maafw.log 增量尾部读取器（带轮转重置）。

    增量逻辑复用 `infra/logtail.py`（阶段 5 去重，与 assist 的 ndjson 尾读同一份）。
    这里保留「每次从头读完」的语义（`partial_line=False`）：maafw.log 是逐行日志，
    上层按行解析失败就跳过，不需要等半行写完。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._offset = 0

    def read_new_lines(self) -> list[str]:
        data, self._offset = read_new_bytes(self.path, self._offset, partial_line=False)
        if not data:
            return []
        return data.decode("utf-8", errors="replace").splitlines()


class StreamService:
    """后台采样线程 + 事件解析,写 StreamStore。"""

    def __init__(
        self,
        *,
        log_path: Path,
        window_probe: Callable[[], dict[str, Any]],
        detector: Any | None,
        detector_operator: Any | None = None,
        settings: dict[str, Any],
        store: StreamStore | None = None,
        control: Any | None = None,
    ):
        self.store = store or StreamStore(
            max_entries=int(settings.get("stream_max_entries") or 300)
        )
        self._tailer = _LogTailer(log_path)
        self._window_probe = window_probe
        self._detector = detector
        #: 干员专精模型（单类 operator）。与主 detector 并行跑，结果合并入流。
        #: None = 未配置（maa.yolo_operator_model 为空）。
        self._detector_operator = detector_operator
        self._control = control  # M1:ControlClient(可选);事件轮询失败静默跳过
        self._control_cursor = -1  # -1=尚未同步,首次拉取用 latest 对齐(不重放历史)
        self._control_tick = 0
        self._yolo_interval = float(settings.get("yolo_interval_seconds") or 0)  # 0=关
        self._window_interval = float(settings.get("window_interval_seconds") or 5)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_node_at = 0.0
        self._last_node = ""

    # ------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="maaend-stream", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            # 宿主只给 1.5s 关闭宽限；join 必须比它短，否则一旦采样线程正卡在
            # 一次慢 window_probe / M1 HTTP 调用里，宿主就会记
            # "didn't stop gracefully within 1.5s, terminating"。
            # 线程是 daemon，超时后交给宿主终止即可，不必等满。
            t.join(timeout=1.0)
        self._thread = None
        for det in (self._detector, self._detector_operator):
            if det is not None and hasattr(det, "stop"):
                try:
                    det.stop()
                except Exception:  # noqa: BLE001 - 收尾失败不影响退出
                    pass

    # ------------------------------------------------------------- 主循环

    def _loop(self) -> None:
        next_window = 0.0
        next_yolo = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            # 1) 日志事件(每轮都读,廉价)
            try:
                self._drain_log()
            except Exception as exc:  # noqa: BLE001 - 采样永不中断
                self.store.append("tasks", "tailer_error", {"error": str(exc)[:120]})
            # 2) 窗口采样
            if now >= next_window:
                next_window = now + self._window_interval
                self._sample_window()
            # 3) YOLO 采样
            if self._yolo_interval > 0 and now >= next_yolo:
                next_yolo = now + self._yolo_interval
                self._sample_yolo()
            # 4) M1 控制服务事件(每 ~2s;失败静默,M2 事件仍走日志 tailer)
            self._control_tick += 1
            if self._control is not None and self._control_tick % 4 == 0:
                self._drain_control()
            self._stop.wait(0.5)

    def _drain_control(self) -> None:
        """从 MaaEndControl /events 拉增量,归一化入流(kinds 与日志侧一致)。"""
        try:
            since = max(0, self._control_cursor)
            resp = self._control.events(since)
        except Exception:  # noqa: BLE001 - 服务不在线属常态,静默跳过
            return
        entries = resp.get("entries") or []
        if self._control_cursor < 0:
            # 首次拉取:游标对齐 latest,不重放服务侧历史缓冲
            self._control_cursor = int(resp.get("latest_id") or 0)
            return
        for ev in entries:
            kind = str(ev.get("kind") or "control")
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            self.store.append("tasks", kind, dict(data, source="m1"))
        self._control_cursor = int(resp.get("latest_id") or self._control_cursor)

    # ------------------------------------------------------------- 采样器

    def _drain_log(self) -> None:
        for line in self._tailer.read_new_lines():
            m = _TASK_RE.search(line)
            if m:
                self.store.append(
                    "tasks",
                    "task_" + m.group(2).lower(),
                    {"entry": m.group(3), "log_time": m.group(1)},
                )
                continue
            nm = _NODE_RE.search(line)
            if nm:
                node = nm.group(1)
                # 节点行极密:同节点 2 秒内只记一次推进
                if node != self._last_node or time.time() - self._last_node_at > 2:
                    self._last_node = node
                    self._last_node_at = time.time()
                    self.store.append("tasks", "node", {"node": node})

    def _sample_window(self) -> None:
        try:
            info = self._window_probe()
            self.store.append("window", "sample", info)
        except Exception as exc:  # noqa: BLE001
            self.store.append("window", "error", {"error": str(exc)[:120]})

    def _detect_both(self, hwnd: int) -> dict[str, Any]:
        """主 detector + 干员专精 detector，合并 objects。

        为什么合并而不是各存一份：`maa_stream_read` / `ef_yolo_detect` 的消费方
        只认一个 objects 数组，拆成两条流会让所有调用方都要改。这里给每个框打
        `source` 标记（main / operator），需要区分的调用方按字段筛即可。

        干员专精结果**排在前面**：单类模型在 operator 上的精度更高（P 0.953），
        同一目标两路都报时，下游按顺序取第一个会拿到更准的那个。

        专精模型失败不影响主路：单类 small model 崩了不该拖垮整个数据流。
        """
        resp = self._detector.detect_hwnd(hwnd)
        if not resp.get("ok"):
            return resp
        objects = [dict(o, source="main") for o in (resp.get("objects") or [])]
        op = self._detector_operator
        if op is not None:
            try:
                oresp = op.detect_hwnd(hwnd)
                if oresp.get("ok"):
                    objects[:0] = [dict(o, source="operator") for o in (oresp.get("objects") or [])]
                else:
                    objects[:0] = [{"label": "operator_model_error",
                                    "source": "operator",
                                    "error": str(oresp.get("error"))[:80]}]
            except Exception as exc:  # noqa: BLE001
                objects[:0] = [{"label": "operator_model_error", "source": "operator",
                                "error": str(exc)[:80]}]
        return {**resp, "count": len(objects), "objects": objects,
                "operator_enabled": op is not None}

    def _sample_yolo(self) -> None:
        if self._detector is None:
            return
        try:
            hwnd = self._window_probe().get("hwnd") or 0
            if not hwnd:
                self.store.append("yolo", "skip", {"reason": "window_offline"})
                return
            resp = self._detect_both(hwnd)
            if resp.get("ok"):
                self.store.append(
                    "yolo", "detect",
                    {"size": resp.get("size"), "count": resp.get("count"),
                     "objects": (resp.get("objects") or [])[:20]},
                )
            else:
                self.store.append("yolo", "error", {"error": str(resp.get("error"))[:120]})
        except Exception as exc:  # noqa: BLE001 - 采样永不中断
            self.store.append("yolo", "error", {"error": "sample: " + str(exc)[:100]})

    # ------------------------------------------------------------- 即时检测(工具调用)

    def detect_now(self) -> dict[str, Any]:
        """按需即时检测(sidecar 全包截图+推理),结果同时入流。"""
        if self._detector is None:
            return {"error": "yolo 未配置(maa.yolo_model)"}
        info = self._window_probe()
        if not info.get("found"):
            return {"error": "游戏窗口不在线"}
        resp = self._detect_both(int(info["hwnd"]))
        if not resp.get("ok"):
            return {"error": str(resp.get("error") or "sidecar 拒绝")}
        payload = {
            "size": resp.get("size"),
            "count": resp.get("count"),
            "objects": resp.get("objects") or [],
            "operator_enabled": bool(resp.get("operator_enabled")),
            "ts": round(time.time(), 3),
        }
        if resp.get("version_warning"):
            payload["version_warning"] = resp["version_warning"]
        self.store.append("yolo", "detect_now", payload)
        return payload
