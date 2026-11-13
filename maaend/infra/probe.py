"""安装探测：把"这台机器上有没有可用的 MaaEnd"变成结构化事实。

MaaEnd（https://github.com/MaaEnd/MaaEnd）是《明日方舟：终末地》的 MAA 助手桌面应用。
发布包布局（由它的 CI install.yml 决定）：

    <安装根>/
    ├── MaaEnd.exe            ← 应用本体（MXU/WebView2 宿主，无对外控制 API）
    ├── maafw/                ← 捆绑的 MaaFramework 运行库（MaaFramework.dll 等）
    ├── interface.json        ← MAA 标准任务清单（JSON5，任务由 CI 在发布时填充）
    ├── resource/<名称>/…     ← 流水线资源
    └── agent/{go-service,cpp-algo}/  ← MaaFramework AgentServer 子进程

探测只陈述事实（在不在、是什么版本、缺什么），不猜原因。
"""

from __future__ import annotations

import glob
import os
import re
from pathlib import Path
from typing import Any

#: 常见安装位置（Windows）。用户可在配置里用 maaend_dir 覆盖。带 * 的按通配展开（新解压的优先）。
COMMON_DIRS = (
    r"%LOCALAPPDATA%\Programs\MaaEnd",
    r"%LOCALAPPDATA%\MaaEnd",
    r"%ProgramFiles%\MaaEnd",
    r"D:\MaaEnd",
    r"D:\Program Files\MaaEnd",
    r"%USERPROFILE%\Downloads\MaaEnd*",
)

_DLLS = ("MaaFramework.dll", "MaaToolkit.dll")


def _expand(text: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(text)))


def _first_exe(root: Path) -> str:
    """安装根下的应用主程序：优先名字带 maa/end/mxu 的，否则取唯一 exe。"""
    try:
        exes = [p for p in root.iterdir() if p.suffix.lower() == ".exe"]
    except OSError:
        return ""
    named = [p for p in exes if re.search(r"maa|end|mxu", p.stem, re.IGNORECASE)]
    return str(named[0] if named else (exes[0] if len(exes) == 1 else ""))


def _find_interface_json(root: Path) -> str:
    for candidate in (root / "interface.json", root / "resource" / "interface.json"):
        if candidate.is_file():
            return str(candidate)
    return ""


def _resource_dirs(root: Path) -> list[str]:
    base = root / "resource"
    if not base.is_dir():
        return []
    # 新版 MaaEnd 把 pipeline/ 直接放在 resource/ 下;旧版是 resource/<bundle>/pipeline
    candidates = [base, *(p for p in base.iterdir() if p.is_dir())]
    return [str(p) for p in candidates if (p / "pipeline").is_dir()]


def find_install(override: str = "") -> dict[str, Any]:
    """探测 MaaEnd 安装。override 为空时按常见位置搜索。"""
    candidates: list[Path] = []
    if override:
        candidates.append(_expand(override))
    else:
        for text in COMMON_DIRS:
            path = _expand(text)
            if "*" in text:
                hits = sorted(glob.glob(str(path)), key=os.path.getmtime, reverse=True)
                candidates.extend(Path(h) for h in hits)
            else:
                candidates.append(path)

    searched: list[str] = []
    partial: dict[str, Any] | None = None
    for root in candidates:
        searched.append(str(root))
        if not root.is_dir():
            continue
        exe = _first_exe(root)
        maafw = root / "maafw" / "bin"
        if not maafw.is_dir():
            maafw = root / "maafw"
        dlls = {name: str(maafw / name) for name in _DLLS}
        dll_ok = all(Path(v).is_file() for v in dlls.values())
        interface = _find_interface_json(root)
        install = {
            "found": bool(exe and dll_ok and interface),
            "root": str(root),
            "app_exe": exe,
            "maafw_bin": str(maafw) if maafw.is_dir() else "",
            "dlls": dlls,
            "runtime_ready": dll_ok,
            "interface_json": interface,
            "resource_dirs": _resource_dirs(root),
            "searched": searched,
        }
        if install["found"] or override:
            return install
        partial = partial or install  # 自动探测：先找完整安装，都不完整再报第一个残缺目录

    if partial is not None:
        return partial
    return {
        "found": False,
        "root": "",
        "app_exe": "",
        "maafw_bin": "",
        "dlls": {},
        "runtime_ready": False,
        "interface_json": "",
        "resource_dirs": [],
        "searched": searched,
    }


# --------------------------------------------------------------------------- #
# 游戏窗口（Win32）。MaaEnd 的控制器按 UnityWndClass + 标题含 Endfield 找窗口。
# 这里用标准库 ctypes 直接枚举，规则可由配置覆盖。
# --------------------------------------------------------------------------- #

_USER32 = None


def _user32():
    global _USER32
    if _USER32 is None:
        import ctypes

        _USER32 = ctypes.WinDLL("user32", use_last_error=True)
    return _USER32


def find_game_window(class_regex: str, title_regex: str) -> dict[str, Any]:
    """枚举桌面顶层窗口，按类名/标题正则过滤。返回 {found, hwnd, title, class}。"""
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    found: dict[str, Any] = {"found": False, "hwnd": 0, "title": "", "class": ""}
    class_re = re.compile(class_regex or "", re.IGNORECASE)
    title_re = re.compile(title_regex or "", re.IGNORECASE)

    results: list[tuple[int, str, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _on_window(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        results.append((hwnd, buf.value, cls.value))
        return True

    user32.EnumWindows(_on_window, 0)

    for hwnd, title, cls in results:
        if class_re.search(cls) and title_re.search(title):
            found = {"found": True, "hwnd": int(hwnd), "title": title, "class": cls}
            return found
    return found


# --------------------------------------------------------------------------- #
# 游戏本体安装（verify_game 用）：终末地随鹰角启动器装在 <启动器根>/games/ 下，
# 启动器根从卸载注册表（DisplayName 含"鹰角"/Hypergryph）的 InstallLocation 读。
# --------------------------------------------------------------------------- #

GAME_EXE_REL = Path("games") / "Arknights Endfield" / "Endfield.exe"
LAUNCHER_DIRS = (r"%ProgramFiles%\Hypergryph Launcher", r"D:\Program Files\Hypergryph Launcher")
_UNINSTALL = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
              r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")


def _launcher_roots() -> list[str]:
    try:
        import winreg
    except ImportError:  # 非 Windows
        return []
    roots: list[str] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for base in _UNINSTALL:
            try:
                key = winreg.OpenKey(hive, base)
            except OSError:
                continue
            with key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                            name = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                            if "鹰角" in name or "Hypergryph" in name:
                                roots.append(str(winreg.QueryValueEx(sub, "InstallLocation")[0]))
                    except OSError:
                        continue
    return roots


def find_game_install() -> dict[str, Any]:
    """探测终末地本体安装。返回 {found, exe, searched}。"""
    searched: list[str] = []
    for root in [*_launcher_roots(), *(str(_expand(d)) for d in LAUNCHER_DIRS)]:
        exe = Path(root) / GAME_EXE_REL
        searched.append(str(exe))
        if exe.is_file():
            return {"found": True, "exe": str(exe), "searched": searched}
    return {"found": False, "exe": "", "searched": searched}


def verify_enabled(mode: str, game_install: dict[str, Any]) -> bool:
    """verify_game：auto = 检测到游戏安装才开；on / off 强制。"""
    mode = str(mode or "auto").strip().lower()
    return bool(game_install.get("found")) if mode == "auto" else mode == "on"


def same_exe(a: str, b: str) -> bool:
    return bool(a and b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def window_exe(hwnd: int) -> str:
    """hwnd 所属进程的 exe 全路径；拿不到（进程受保护/已退出）返回空串。"""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    handle = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        ok = k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return buf.value if ok else ""
    finally:
        k32.CloseHandle(handle)


def count_windows() -> int:
    """桌面可见顶层窗口数——验证枚举代码路径本身是否工作。"""
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    count = 0

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _on_window(hwnd, _lparam):
        nonlocal count
        if user32.IsWindowVisible(hwnd):
            count += 1
        return True

    user32.EnumWindows(_on_window, 0)
    return count


def foreground_hwnd() -> int:
    """当前前台窗口 hwnd（OCR 轮询只在游戏前台时抓屏，不抢焦点）。"""

    return int(_user32().GetForegroundWindow() or 0)


