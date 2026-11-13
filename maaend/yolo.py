"""maa_end YOLO 视觉层 —— 检测客户端与实时调度。

设计约束:
- ultralytics/torch 只在首次检测时懒加载(插件启动零开销);
- 截图不再在本模块实现 —— 统一走 `vision/capture.py`(2026-11-04 重构:原先
  `capture_window` / `yolo_sidecar.capture` / `ocr_sidecar._from_hwnd` 三份重复);
- 真实推理在系统 Python 子进程里跑(宿主是 Nuitka 冻结 exe,ultralytics 导入链
  必经 matplotlib,而 matplotlib 被 --nofollow-import-to 排除 —— 见 yolo_sidecar);
- 一切阻塞调用(capture/infer)由调用方放线程;本模块不做线程管理。
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from .vision.capture import CaptureError, capture, capture_window


class YoloError(RuntimeError):
    """截图或检测失败。"""


# ---------------------------------------------------------------- latest-wins


class LatestWinsDetector:
    """把「同步一问一答」的 runner 包成「只处理最新请求」。

    实时循环里检测耗时常常大于采帧间隔,请求会堆在链路上、结果全部滞后。
    这里用**单槽信箱**:新请求覆盖尚未处理的旧请求;已被更新的请求立刻
    拿到 {"ok": False, "superseded": True} 返回,不再占用链路。

    语义与上游 Endfield 的「接收端始终只发布最新的完整帧」一致 —— 帧是
    可以丢的,延迟不能堆。

    ⚠️ 两个必须知道的语义细节:
    1. **正在处理中的请求也会被判 superseded**:一旦有更新的请求提交,
       在途那个的结果交付时已是陈旧数据,所以调用方立刻被放行(不必干等
       一个过期结果)。底层那次计算无法取消,会跑完但结果作废。
    2. **最新请求必定拿到真实结果**:信箱里永远留着最新的一条,所以不会
       出现"所有调用方都被 superseded"的活锁。
    """

    def __init__(self, runner: Callable[[dict], dict]):
        self._run = runner
        self._cond = threading.Condition()
        self._slot: tuple[int, dict] | None = None   # 待处理的最新请求
        self._results: dict[int, dict] = {}
        self._seq = 0
        self._latest_seq = 0
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def submit(self, req: dict) -> dict:
        """提交请求,等待结果;若中途被新请求取代则返回 superseded。"""
        with self._cond:
            self._seq += 1
            seq = self._seq
            self._latest_seq = seq
            self._slot = (seq, req)
            self._cond.notify_all()
            while True:
                if seq in self._results:
                    return self._results.pop(seq)
                if self._latest_seq > seq:
                    return {"ok": False, "error": "superseded", "superseded": True}
                self._cond.wait()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while self._slot is None and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                seq, req = self._slot
                self._slot = None
            try:
                resp = self._run(req)
            except Exception as exc:                  # 绝不因一次异常让线程死掉
                resp = {"ok": False, "error": str(exc)[:120]}
            with self._cond:
                self._results[seq] = resp
                self._cond.notify_all()

    @property
    def pending(self) -> bool:
        with self._cond:
            return self._slot is not None

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()


class SidecarDetector:
    """经系统 Python 子进程跑 YOLO(宿主冻结进程 matplotlib 被 Nuitka 排除,
    ultralytics 在插件进程内永远不可导入 —— 2026-09-27 实测)。

    协议见 yolo_sidecar.py;进程懒启动,死了自动重启;stop() 收尾。
    """

    def __init__(self, model_path: Path, confidence: float = 0.4, python_exe: str = "python"):
        import shutil
        self.model_path = Path(model_path)
        self.confidence = float(confidence)
        self.python_exe = python_exe if Path(python_exe).is_file() else (
            shutil.which("python") or "python"
        )
        self._proc = None
        self._lw: LatestWinsDetector | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- 进程

    def _running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start(self):
        here = Path(__file__).resolve().parent / "yolo_sidecar.py"
        self._proc = subprocess.Popen(
            [self.python_exe, "-X", "utf8", str(here),
             str(self.model_path), str(self.confidence)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        banner = self._proc.stdout.readline()
        try:
            info = json.loads(banner)
            if not info.get("ok"):
                raise YoloError(str(info.get("error") or "sidecar 启动横幅报错"))
        except ValueError as exc:
            raise YoloError("sidecar 横幅不是 JSON: " + banner[:100] + " / " + str(exc))

    def _ask(self, req: dict) -> dict:
        with self._lock:
            if not self._running():
                self._start()
            try:
                self._proc.stdin.write(
                    (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
                )
                self._proc.stdin.flush()
                line = self._proc.stdout.readline()
                return json.loads(line)
            except (OSError, ValueError) as exc:
                # 进程死了/响应坏:清掉,下次重启
                self._kill()
                return {"ok": False, "error": "sidecar 通信失败: " + str(exc)[:120]}

    def _kill(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
            self._proc = None

    def stop(self) -> None:
        with self._lock:
            if self._lw is not None:
                self._lw.stop()
                self._lw = None
            self._kill()

    # ------------------------------------------------------------- 对外

    def detect_hwnd(self, hwnd: int) -> dict[str, Any]:
        """截图+检测全在 sidecar;返回 sidecar 响应(ok/size/count/objects 或 error)。"""
        if not self.model_path.is_file():
            return {"ok": False, "error": "模型不存在: " + str(self.model_path)}
        return self._ask({"cmd": "detect", "hwnd": int(hwnd)})

    def detect_hwnd_latest(self, hwnd: int) -> dict[str, Any]:
        """实时循环用:只处理最新一帧,过期的直接 superseded 返回。"""
        if not self.model_path.is_file():
            return {"ok": False, "error": "模型不存在: " + str(self.model_path)}
        with self._lock:
            if self._lw is None:
                self._lw = LatestWinsDetector(self._ask)
            lw = self._lw
        return lw.submit({"cmd": "detect", "hwnd": int(hwnd)})

    def ocr_hwnd(self, hwnd: int, roi: list | None = None) -> dict[str, Any]:
        """截图+OCR 全在 sidecar;roi=[x1,y1,x2,y2] 0-1 比例可选。"""
        req: dict[str, Any] = {"cmd": "ocr", "hwnd": int(hwnd)}
        if roi:
            req["roi"] = [float(v) for v in roi[:4]]
        return self._ask(req)

    def ping(self) -> bool:
        return bool(self._ask({"cmd": "ping"}).get("pong"))


__all__ = [
    "CaptureError",
    "LatestWinsDetector",
    "SidecarDetector",
    "YoloError",
    "capture",
    "capture_window",
]
