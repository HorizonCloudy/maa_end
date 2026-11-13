"""Win32 窗口截图 —— 全插件唯一实现。

历史：这段代码原本有三份（`yolo.capture_window` / `yolo_sidecar.capture` /
`ocr_sidecar._from_hwnd` 间接复用第二份），逐行同构。重构（docs/REFACTOR-PLAN.md
阶段 1）合并到这里，三处全部改为 import 本模块。

实现选择：**屏幕 DC BitBlt**（与 MaaFramework 的 `interface.screencap=ScreenDC` 同款），
不是 PrintWindow —— 本游戏是 Unity/D3D 窗口，PrintWindow 实测抓不到内容。

诚实局限（与框架行为一致，不要在调用方"修补"）：
- 窗口必须在屏上；最小化会先 `SW_RESTORE` 并等 0.4s 让 DWM 出帧；
- 窗口被其他窗口遮挡时，抓到的是遮挡者；
- 截图会 `SetForegroundWindow` 把游戏提到前台 —— 只读陪伴场景可接受，
  但不要在高频循环里连续调用（会抢焦点）。

本模块只用标准库，可被主进程与 sidecar 子进程同时导入：

    主进程（包内）：  from ..vision.capture import capture
    worker（顶层）：  sys.path.insert(0, <maaend 目录>); from vision.capture import capture
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

#: 最小化恢复后等待 DWM 出帧的时间（秒）。
RESTORE_SETTLE_S = 0.4
#: 提到前台后等待画面稳定的时间（秒）。
FOREGROUND_SETTLE_S = 0.15


class CaptureError(RuntimeError):
    """截图失败（窗口无效 / GDI 调用失败 / 客户区为空）。"""


_GDI32: ctypes.WinDLL | None = None
_USER32: ctypes.WinDLL | None = None


def _libs() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    global _GDI32, _USER32
    if _GDI32 is None:
        _GDI32 = ctypes.WinDLL("gdi32")
        _USER32 = ctypes.WinDLL("user32")
    return _GDI32, _USER32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def capture(hwnd: int) -> tuple[int, int, bytes]:
    """抓窗口客户区，返回 (width, height, BGR bytes)。

    BGR 是 24bpp、top-down（`biHeight = -h`）的原始字节，行间无 padding ——
    可直接 `numpy.frombuffer(...).reshape(h, w, 3)`。
    """
    gdi, user = _libs()
    if user.IsIconic(ctypes.c_void_p(hwnd)):
        user.ShowWindow(ctypes.c_void_p(hwnd), 9)  # SW_RESTORE
        time.sleep(RESTORE_SETTLE_S)

    client = wt.RECT()
    if not user.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(client)):
        raise CaptureError("GetClientRect 失败")
    pt = wt.POINT(0, 0)
    user.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(pt))
    w = client.right - client.left
    h = client.bottom - client.top
    if w <= 0 or h <= 0:
        raise CaptureError("客户区为空")

    user.SetForegroundWindow(ctypes.c_void_p(hwnd))
    time.sleep(FOREGROUND_SETTLE_S)

    screen_dc = user.GetDC(None)
    if not screen_dc:
        raise CaptureError("GetDC(screen) 失败")
    try:
        mem_dc = gdi.CreateCompatibleDC(ctypes.c_void_p(screen_dc))
        if not mem_dc:
            raise CaptureError("CreateCompatibleDC 失败")
        try:
            hbmp = gdi.CreateCompatibleBitmap(ctypes.c_void_p(screen_dc), w, h)
            if not hbmp:
                raise CaptureError("CreateCompatibleBitmap 失败")
            try:
                gdi.SelectObject(ctypes.c_void_p(mem_dc), ctypes.c_void_p(hbmp))
                ok = gdi.BitBlt(
                    ctypes.c_void_p(mem_dc), 0, 0, w, h,
                    ctypes.c_void_p(screen_dc), pt.x, pt.y,
                    0x00CC0020 | 0x40000000,  # SRCCOPY | CAPTUREBLT
                )
                if not ok:
                    raise CaptureError("BitBlt 失败")
                bmi = BITMAPINFO()
                bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                bmi.bmiHeader.biWidth = w
                bmi.bmiHeader.biHeight = -h  # top-down
                bmi.bmiHeader.biPlanes = 1
                bmi.bmiHeader.biBitCount = 24
                bmi.bmiHeader.biCompression = 0
                buf = ctypes.create_string_buffer(w * h * 3)
                rows = gdi.GetDIBits(
                    ctypes.c_void_p(mem_dc), ctypes.c_void_p(hbmp), 0, h,
                    buf, ctypes.byref(bmi), 0,
                )
                if rows != h:
                    raise CaptureError(f"GetDIBits 不完整: {rows}/{h}")
                return w, h, buf.raw
            finally:
                gdi.DeleteObject(ctypes.c_void_p(hbmp))
        finally:
            gdi.DeleteDC(ctypes.c_void_p(mem_dc))
    finally:
        user.ReleaseDC(None, ctypes.c_void_p(screen_dc))


#: 旧名兼容：`yolo.capture_window` 的调用点还在用这个名字。
capture_window = capture
