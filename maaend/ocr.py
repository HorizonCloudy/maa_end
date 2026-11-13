"""maa_end OCR 层 —— 插件侧客户端(经 sidecar 跑 RapidOCR)。

识别层第二路,与 YOLO 互补:
- YOLO 认"是什么":框 + 类(enemy/operator/ui_button…)
- OCR  认"写了什么":文字行 + 客户区/屏幕坐标 + 本体归类

本体(data/yolo/wiki_ontology.json)命中时把文字行归到 enemy_name /
operator_name / item_name —— 类名与 YOLO 训练集同名,上层可以同时拿到
"敌人框"和"敌人名"。仅依赖标准库;推理全在系统 Python 子进程。

阻塞点只有 sidecar 的 readline,调用方需放线程(asyncio.to_thread)。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any


class OcrError(RuntimeError):
    """截图或识别失败。"""


class SidecarOcr:
    """经系统 Python 子进程跑 OCR(宿主冻结进程装不下 onnxruntime 导入链)。

    协议见 ocr_sidecar.py;进程懒启动,死了下次自动重启;stop() 收尾。
    """

    def __init__(
        self,
        ontology: Path | str = "",
        min_confidence: float = 0.55,
        python_exe: str = "python",
    ):
        self.ontology = Path(ontology) if ontology else None
        self.min_confidence = float(min_confidence)
        self.python_exe = (
            python_exe
            if Path(python_exe).is_file()
            else (shutil.which("python") or "python")
        )
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- 进程

    def _running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start(self) -> None:
        here = Path(__file__).resolve().parent / "ocr_sidecar.py"
        self._proc = subprocess.Popen(
            [
                self.python_exe, "-X", "utf8", str(here),
                str(self.min_confidence),
                str(self.ontology or ""),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        banner = self._proc.stdout.readline()
        try:
            info = json.loads(banner)
        except ValueError as exc:
            raise OcrError("sidecar 横幅不是 JSON: " + banner[:100] + " / " + str(exc))
        if not info.get("ok"):
            raise OcrError(str(info.get("error") or "sidecar 启动横幅报错"))

    def _ask(self, req: dict) -> dict:
        with self._lock:
            if not self._running():
                try:
                    self._start()
                except (OcrError, OSError) as exc:
                    self._kill()  # Popen 半途死掉时清句柄,下次调用自动重试
                    return {"ok": False,
                            "error": "OCR sidecar 启动失败: " + str(exc)[:120]}
            try:
                self._proc.stdin.write(
                    (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
                )
                self._proc.stdin.flush()
                return json.loads(self._proc.stdout.readline())
            except (OSError, ValueError) as exc:
                self._kill()
                return {"ok": False, "error": "OCR sidecar 通信失败: " + str(exc)[:120]}

    def _kill(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
            self._proc = None

    def stop(self) -> None:
        with self._lock:
            self._kill()

    # ------------------------------------------------------------- 对外

    def read_hwnd(self, hwnd: int, roi: list[int] | None = None,
                  scale: float = 1.0) -> dict[str, Any]:
        """游戏窗口全画面文字。roi=[x1,y1,x2,y2] 裁剪提速,scale<1 降采样;
        返回坐标均已映射回全客户区。"""
        if not hwnd:
            return {"ok": False, "error": "hwnd 为空"}
        req: dict[str, Any] = {"cmd": "ocr", "hwnd": int(hwnd)}
        if roi:
            req["roi"] = [int(v) for v in roi]
        if scale != 1.0:
            req["scale"] = float(scale)
        return self._ask(req)

    def find(self, hwnd: int, text: str, roi: list[int] | None = None,
             scale: float = 1.0) -> dict[str, Any]:
        """找词:返回 {found, match:{text, box, center, screen, tag, cls}}。"""
        if not hwnd:
            return {"ok": False, "error": "hwnd 为空"}
        if not str(text or "").strip():
            return {"ok": False, "error": "text 为空"}
        req: dict[str, Any] = {"cmd": "find", "hwnd": int(hwnd), "text": str(text)}
        if roi:
            req["roi"] = [int(v) for v in roi]
        if scale != 1.0:
            req["scale"] = float(scale)
        return self._ask(req)

    def read_image(self, path: Path | str) -> dict[str, Any]:
        """离线图片识别(奖励截图 / 采集帧),无屏幕坐标。"""
        p = Path(path)
        if not p.is_file():
            return {"ok": False, "error": "图片不存在: " + str(p)[:120]}
        return self._ask({"cmd": "ocr_image", "path": str(p)})

    def ping(self) -> bool:
        return bool(self._ask({"cmd": "ping"}).get("pong"))