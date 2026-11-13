"""maa_end GameScaling 输入注入 —— M6 执行后端（Win32 SendInput，ctypes）。

把 :class:`maaend.gamescaling.action.Action`（一个 200ms 键鼠块）变成真实的
键盘/鼠标事件：

- 6 个 33ms 子步，每步开始切到该步的按键集合（连续步出现的键持续按住、
  不重按，空组释放全部——训练语义）；块结束（t≈200ms）统一抬起仍在按的键：
  一个块是完整单元，下一个决策迟到/失败时不留卡键；
- 鼠标位移 X/Y（屏幕千分比）按窗口客户区宽高换算成像素，均摊到 6 步，
  误差扩散保证总和精确；
- 滚轮 Z 在第 0 步一次发出（Z>0 向上，与训练口径一致；Windows 每格 120）。

分层：``plan_chunk`` 是纯函数（差分/数学，可离线单测）；``execute_chunk`` /
``release_all`` 才碰 SendInput。阻塞执行一个块约 200ms，调用方放线程
（gamescaling_agent 经 asyncio.to_thread）。

安全：``release_all`` 幂等，熔断/停止/卸载时调用，防按键卡死。SendInput
不需要管理员权限，注入目标是当前前台窗口（capture_window 每轮把游戏
窗口置前台）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from dataclasses import dataclass

try:  # 插件内按包加载（maaend.gamescaling_io）
    from .gamescaling.action import KEY_VOCAB, MOUSE_BUTTON_KEYS, Action
    from .gamescaling.keymaps import TO_WIN32_VK
except ImportError:  # 纯逻辑单测按顶层模块加载（sys.path 挂 maaend/）
    from backends.gamescaling.action import KEY_VOCAB, MOUSE_BUTTON_KEYS, Action
    from backends.gamescaling.keymaps import TO_WIN32_VK

# ---------------------------------------------------------------- 结构与常量

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120  # Windows 滚轮一格 = 120


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _INPUTUNION)]


_USER32: ctypes.WinDLL | None = None


def _user32() -> ctypes.WinDLL:
    global _USER32
    if _USER32 is None:
        _USER32 = ctypes.WinDLL("user32")
    return _USER32


def _send(*events: _INPUT) -> None:
    if not events:
        return
    arr = (_INPUT * len(events))(*events)
    sent = _user32().SendInput(len(events), arr, ctypes.sizeof(_INPUT))
    if sent != len(events):  # 被 UIPI 等拒绝：如实抛出，代理循环据此熔断
        raise OSError(f"SendInput 只注入了 {sent}/{len(events)} 个事件")


def _key_event(vk: int, up: bool) -> _INPUT:
    return _INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=_KEYBDINPUT(
        wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0, time=0,
        dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))))


def _mouse_event(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _INPUT:
    return _INPUT(INPUT_MOUSE, _INPUTUNION(mi=_MOUSEINPUT(
        dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0,
        dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)))))


_BUTTON_DOWN = {"LB": MOUSEEVENTF_LEFTDOWN, "RB": MOUSEEVENTF_RIGHTDOWN,
                "MB": MOUSEEVENTF_MIDDLEDOWN}
_BUTTON_UP = {"LB": MOUSEEVENTF_LEFTUP, "RB": MOUSEEVENTF_RIGHTUP,
              "MB": MOUSEEVENTF_MIDDLEUP}


# ---------------------------------------------------------------- 纯计划（可单测）

@dataclass(frozen=True)
class StepPlan:
    """一个 33ms 子步边界要发生的事（token 口径，与后端无关）。"""

    press: tuple[str, ...] = ()
    release: tuple[str, ...] = ()
    dx: int = 0
    dy: int = 0
    wheel: int = 0  # 只在第 0 步非 0；Windows 滚轮增量（带符号）


def _spread(per_mille: float, size_px: int, steps: int) -> list[int]:
    """总位移（千分比 × 窗口尺寸）均摊到 steps 份，误差扩散，总和精确。"""
    total = per_mille * size_px / 1000.0
    out, acc = [], 0
    for i in range(1, steps + 1):
        step = round(total * i / steps) - acc
        acc += step
        out.append(step)
    return out


def plan_chunk(action: Action, width: int, height: int) -> list[StepPlan]:
    """Action → 恰好 6 个 StepPlan（步边界差分）。纯函数，不碰系统。

    第 i 步的 press/release 是第 i-1 → i 步按键集合的差分（第 0 步对空集）。
    块结束仍在按的键由 execute_chunk 在 t≈200ms 统一抬起，不在计划里。
    """
    if not action.parsed:
        return [StepPlan() for _ in range(6)]
    dxs = _spread(action.mouse[0], width, 6)
    dys = _spread(action.mouse[1], height, 6)
    wheel = int(round(action.mouse[2])) * WHEEL_DELTA
    plans: list[StepPlan] = []
    held: set[str] = set()
    for i, group in enumerate(action.steps[:6]):
        cur = set(group)
        plans.append(StepPlan(
            press=tuple(sorted(cur - held)),
            release=tuple(sorted(held - cur)),
            dx=dxs[i], dy=dys[i],
            wheel=wheel if i == 0 else 0,
        ))
        held = cur
    return plans


# ---------------------------------------------------------------- 真实注入

def _token_events(tokens, up: bool) -> list[_INPUT]:
    out: list[_INPUT] = []
    for tok in tokens:
        if tok in MOUSE_BUTTON_KEYS:
            out.append(_mouse_event(_BUTTON_UP[tok] if up else _BUTTON_DOWN[tok]))
        else:
            out.append(_key_event(TO_WIN32_VK[tok], up))
    return out


def execute_chunk(plans: list[StepPlan], *, step_ms: float = 33.3) -> None:
    """阻塞执行一个 200ms 块（6 × 33ms，perf_counter 对齐防漂移）。

    末尾抬起本块按下的全部键：块是完整单元，下一决策迟到/失败不留卡键。
    """
    t0 = time.perf_counter()
    held: set[str] = set()
    for i, plan in enumerate(plans):
        events = [*_token_events(plan.press, False), *_token_events(plan.release, True)]
        if plan.dx or plan.dy:
            events.append(_mouse_event(MOUSEEVENTF_MOVE, plan.dx, plan.dy))
        if plan.wheel:
            events.append(_mouse_event(MOUSEEVENTF_WHEEL, data=plan.wheel))
        _send(*events)
        held = (held | set(plan.press)) - set(plan.release)
        deadline = t0 + (i + 1) * step_ms
        remain = deadline - time.perf_counter()
        if remain > 0:
            time.sleep(remain)
    if held:
        _send(*_token_events(tuple(sorted(held)), True))


def release_all() -> None:
    """幂等抬起全部词表键与三鼠标键（熔断/停止/卸载时防按键卡死）。"""
    events: list[_INPUT] = [_key_event(vk, True) for vk in sorted(TO_WIN32_VK.values())]
    events += [_mouse_event(flag) for flag in (_BUTTON_UP["LB"], _BUTTON_UP["RB"],
                                               _BUTTON_UP["MB"])]
    _send(*events)


def check_vocab() -> None:
    """启动自检：词表内除鼠标三键外的每个 token 必须有 Win32 VK 映射。"""
    missing = KEY_VOCAB - set(MOUSE_BUTTON_KEYS) - TO_WIN32_VK.keys()
    if missing:
        raise ValueError(f"词表 token 缺 Win32 VK 映射: {sorted(missing)}")
