"""maa_end ↔ MaaEndControl 客户端 —— M1 执行后端（DESIGN-maaend-suite-modes.md §2.2）。

MaaEndControl 是 MaaEnd fork 的本地控制服务（agent/control-service，Go，纯标准库）：
HTTP/SSE 暴露任务清单/执行/状态/事件流。本模块只做三件事：

- 健康探测（/health，1s 超时，失败返回 None 不抛）；
- 按需拉起（control_spawn=true 时 spawn 安装目录里的 MaaEndControl.exe 并等端口就绪）；
- 薄封装 GET/POST（/tasks /run /stop /state /events?format=json）。

纯标准库（urllib）；一切失败抛 ControlError，调用方据此降级 M2，绝不拖垮插件。
阻塞调用请放线程（router 里 asyncio.to_thread）。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib import error as _ue
from urllib import request as _rq
from urllib.parse import urlparse


class ControlError(RuntimeError):
    """控制服务不可达 / 拒绝 / 响应异常。"""


class ControlClient:
    """MaaEndControl 的最小 HTTP 客户端（可注入日志与安装根）。"""

    def __init__(
        self,
        base_url: str,
        install_root: str = "",
        spawn: bool = True,
        log: Any = None,
    ) -> None:
        self.base = (base_url or "").rstrip("/")
        self.install_root = str(install_root or "")
        self.spawn = bool(spawn and install_root)
        self._log = log or (lambda msg: None)
        self._proc: subprocess.Popen | None = None  # 本插件拉起的进程

    # ------------------------------------------------------------- 基础

    def _request(self, path: str, body: dict | None = None, timeout: float = 10.0) -> dict:
        if not self.base:
            raise ControlError("control_url 未配置")
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = _rq.Request(
            self.base + path, data=data, headers=headers,
            method="POST" if body is not None else "GET",
        )
        try:
            with _rq.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(8 << 20)
        except _ue.HTTPError as exc:
            # 服务端 4xx/5xx 的 body 是 {"ok":false,"error":"中文原因"}——尽量透出
            try:
                payload = json.loads(exc.read(1 << 20).decode("utf-8"))
                if isinstance(payload, dict) and payload.get("error"):
                    raise ControlError(str(payload["error"])) from exc
            except ControlError:
                raise
            except Exception:  # noqa: BLE001 - body 解析失败退回状态码
                pass
            raise ControlError(f"HTTP {exc.code}: {exc.reason}") from exc
        except ControlError:
            raise
        except Exception as exc:  # noqa: BLE001 - 连接失败等一律降为 ControlError
            raise ControlError(f"{type(exc).__name__}: {exc}") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ControlError(f"响应不是 JSON: {raw[:120]!r}") from exc
        if not isinstance(payload, dict):
            raise ControlError(f"响应不是对象: {type(payload).__name__}")
        if payload.get("ok") is False:
            raise ControlError(str(payload.get("error") or "控制服务拒绝"))
        return payload

    # ------------------------------------------------------------- 端点

    def health(self, timeout: float = 1.0) -> dict | None:
        """存活探测；不可达返回 None（不抛，调用方据此降级）。"""
        try:
            return self._request("/health", timeout=timeout)
        except ControlError:
            return None

    def state(self, timeout: float = 10.0) -> dict:
        return self._request("/state", timeout=timeout)

    def run(self, task: str, options: dict | None = None,
            controller: str = "", adb_serial: str = "") -> dict:
        body: dict = {"task": task, "options": options or {}}
        if controller:
            body["controller"] = controller
        if adb_serial:
            body["adb_serial"] = adb_serial
        return self._request("/run", body, timeout=30.0)

    def stop(self) -> dict:
        return self._request("/stop", {}, timeout=20.0)

    def events(self, since: int = 0) -> dict:
        return self._request(f"/events?format=json&since={max(0, int(since))}", timeout=10.0)

    # ------------------------------------------------------------- 按需拉起

    def exe_path(self) -> str:
        """安装根里的 MaaEndControl.exe（fork 构建产物，官方更新不覆盖）。"""
        if not self.install_root:
            return ""
        p = Path(self.install_root) / "MaaEndControl.exe"
        return str(p) if p.is_file() else ""

    def ensure_started(self, timeout: float = 8.0) -> bool:
        """健康则 True；否则按配置 spawn 并等端口就绪。失败如实返回 False。"""
        if self.health() is not None:
            return True
        if not self.spawn:
            return False
        exe = self.exe_path()
        if not exe:
            self._log("MaaEndControl.exe 不在安装目录（fork 未部署），M1 本次不可用")
            return False
        netloc = urlparse(self.base).netloc or "127.0.0.1:8829"
        try:
            self._proc = subprocess.Popen(
                [exe, "-addr", netloc],
                cwd=str(Path(exe).parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self._log(f"拉起 MaaEndControl 失败：{exc}")
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.health() is not None:
                self._log("MaaEndControl 已由插件拉起")
                return True
            if self._proc.poll() is not None:
                self._log("MaaEndControl 启动后立即退出（看安装目录可执行性/端口占用）")
                return False
            time.sleep(0.5)
        self._log("MaaEndControl 端口等待超时")
        return False

    def stop_spawned(self) -> None:
        """收掉本插件拉起的进程（外部手动启动的不动）。"""
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass
        self._proc = None
