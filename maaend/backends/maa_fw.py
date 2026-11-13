"""MaaFramework C ABI 的最小 ctypes 封装——仅版本查询。

执行任务由官方运行器 MaaPiCli 完成（见 runner.py），ctypes 只在 status 里
读一下运行库版本号。

签名对齐官方 Python 绑定与头文件：Windows 调用约定 ``__stdcall`` →
``ctypes.WinDLL``；``MaaVersion()`` 返回 ``const char*``。

已在两个运行库上实测：MaaFramework v5.14.0 官方 release、
MaaEnd v2.29.0 捆绑的 v5.14.0-beta.1。
"""

from __future__ import annotations

import ctypes
from pathlib import Path


class MaaFwError(RuntimeError):
    """运行库加载或调用失败。"""


class MaaFw:
    """已加载的 MaaFramework 运行库（仅版本查询）。"""

    def __init__(self, bin_dir: str) -> None:
        bin_path = Path(bin_dir)
        if not bin_path.is_dir():
            raise MaaFwError(f"运行库目录不存在：{bin_path}")
        dll = bin_path / "MaaFramework.dll"
        if not dll.is_file():
            raise MaaFwError(f"找不到 MaaFramework.dll：{dll}")
        self.bin_dir = str(bin_path)
        self._add_dll_directory(bin_path)
        try:
            self.fw = ctypes.WinDLL(str(dll))
        except OSError as exc:
            raise MaaFwError(f"加载 MaaFramework.dll 失败：{exc}") from exc
        self.fw.MaaVersion.restype = ctypes.c_char_p
        self.fw.MaaVersion.argtypes = []

    @staticmethod
    def _add_dll_directory(bin_path: Path) -> None:
        try:
            ctypes.windll.kernel32.AddDllDirectory(str(bin_path))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 老系统没有该 API 也不影响（同目录加载兜底）
            pass

    def version(self) -> str:
        return self.fw.MaaVersion().decode("utf-8", "replace")
