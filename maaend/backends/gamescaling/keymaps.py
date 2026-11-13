"""训练键位词表 -> 常见输入后端的键名（vendor 自 THUSI-Lab/GameScaling，Apache-2.0）。

来源：gamescaling/keymaps.py @ 6764efabfbb4121d6e5d3cfcbc54dedf1469bf5a
许可：Apache-2.0，全文见 third_party/GameScaling-LICENSE.txt
改动：仅追加 ``TO_WIN32_VK``（本插件 SendInput 注入用）与本文件头，其余逐字保留。

鼠标键（`LB` / `RB` / `MB`）不是键盘键：下面的表都不含它们，
各后端经 `MOUSE_BUTTONS` 单独处理。
"""

from __future__ import annotations

from .action import LETTER_KEYS

MOUSE_BUTTONS: dict[str, str] = {"LB": "left", "RB": "right", "MB": "middle"}

# Unreal Engine 5 FKey names. Left variants are
# used for the L/R-merged modifier tokens.
TO_UE5: dict[str, str] = {
    **{c: c for c in LETTER_KEYS},
    "zero": "Zero", "one": "One", "two": "Two", "three": "Three", "four": "Four",
    "five": "Five", "six": "Six", "seven": "Seven", "eight": "Eight", "nine": "Nine",
    "One": "F1", "Two": "F2", "Three": "F3", "Four": "F4", "Five": "F5", "Six": "F6",
    "Seven": "F7", "Eight": "F8", "Nine": "F9", "Ten": "F10", "Eleven": "F11",
    "Twelve": "F12",
    "Shift": "LeftShift", "Ctrl": "LeftControl", "Alt": "LeftAlt", "Tab": "Tab",
    "Caps": "CapsLock", "Esc": "Escape", "Space": "SpaceBar", "Enter": "Enter",
    "Back": "BackSpace", "Delete": "Delete", "Insert": "Insert", "Home": "Home",
    "End": "End", "Pause": "Pause",
    "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right",
    "Equal": "Equals", "Minus": "Hyphen", "Period": "Period", "Slash": "Slash",
    "Quote": "Apostrophe",
}

# Playwright `page.keyboard` names (browser games). Letters are lower case.
TO_PLAYWRIGHT: dict[str, str] = {
    **{c: c.lower() for c in LETTER_KEYS},
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "One": "F1", "Two": "F2", "Three": "F3", "Four": "F4", "Five": "F5", "Six": "F6",
    "Seven": "F7", "Eight": "F8", "Nine": "F9", "Ten": "F10", "Eleven": "F11",
    "Twelve": "F12",
    "Shift": "Shift", "Ctrl": "Control", "Alt": "Alt", "Tab": "Tab", "Caps": "CapsLock",
    "Esc": "Escape", "Space": "Space", "Enter": "Enter", "Back": "Backspace",
    "Delete": "Delete", "Insert": "Insert", "Home": "Home", "End": "End", "Pause": "Pause",
    "Up": "ArrowUp", "Down": "ArrowDown", "Left": "ArrowLeft", "Right": "ArrowRight",
    "Equal": "Equal", "Minus": "Minus", "Period": "Period", "Slash": "Slash",
    "Quote": "Quote",
}

# Playwright name -> training token, for rewriting a game's documented controls
# into the vocabulary the model emits.
PLAYWRIGHT_TO_TOKEN: dict[str, str] = {
    "ArrowLeft": "Left", "ArrowRight": "Right", "ArrowUp": "Up", "ArrowDown": "Down",
    "Escape": "Esc", "CapsLock": "Caps", "Control": "Ctrl", "Backspace": "Back",
}

# ---------------------------------------------------------------------------
# 本插件新增：Win32 Virtual-Key codes（user32 SendInput 的 KEYBDINPUT.wVk 用）。
# 修饰键取左侧变体（与 TO_UE5 的 L/R 合并口径一致）；标点走 OEM 键（US 布局）。
# 鼠标三键不在本表——gamescaling_io 走 MOUSEEVENTF 事件。
# ---------------------------------------------------------------------------
TO_WIN32_VK: dict[str, int] = {
    **{c: 0x41 + i for i, c in enumerate(LETTER_KEYS)},          # A..Z
    **{w: 0x30 + i for i, w in enumerate((
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"))},
    **{w: 0x70 + i for i, w in enumerate((
        "One", "Two", "Three", "Four", "Five", "Six",
        "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve"))},  # F1..F12
    "Shift": 0x10, "Ctrl": 0x11, "Alt": 0x12, "Tab": 0x09, "Caps": 0x14,
    "Esc": 0x1B, "Space": 0x20, "Enter": 0x0D, "Back": 0x08, "Pause": 0x13,
    "Delete": 0x2E, "Insert": 0x2D, "Home": 0x24, "End": 0x23,
    "Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27,
    "Equal": 0xBB, "Minus": 0xBD, "Period": 0xBE, "Slash": 0xBF, "Quote": 0xDE,
}
