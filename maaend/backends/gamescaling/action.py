"""GameScaling 动作格式（vendor 自 THUSI-Lab/GameScaling，Apache-2.0）。

来源：gamescaling/action.py @ 6764efabfbb4121d6e5d3cfcbc54dedf1469bf5a
许可：Apache-2.0，全文见 third_party/GameScaling-LICENSE.txt
改动：仅追加本文件头，代码逐字保留——动作格式是训练格式，改语义会破坏模型行为。

每次决策模型输出恰好一个 200ms 键鼠块：

    <|action_start|>X Y Z ; g1 ; g2 ; g3 ; g4 ; g5 ; g6<|action_end|>

- ``X Y`` 是整个 200ms 的鼠标位移，单位是屏幕的千分比
  （``X = sum(dx) / width * 1000``，裁剪到 ±1000；X>0 右移，Y>0 下移）。
- ``Z`` 是滚轮格数（裁剪到 ±5；Z>0 向上）。
- ``g1..g6`` 是六个 33ms 子步，每组列出该步按住的键（至多 4 个）。
  连续组出现的键是持续按住；空组释放全部。

推理模型可能在块前加 ``<think>...</think>``。动作永远从**最后一个**
``</think>`` 之后的文本读取：思考型模型会引用格式本身（"the format is
<|action_start|>X Y Z ; ..."），读全文会把引用当动作执行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ACTION_START = "<|action_start|>"
ACTION_END = "<|action_end|>"
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

CHUNK_MS = 200.0
CHUNK_STEPS = 6
STEP_MS = CHUNK_MS / CHUNK_STEPS
MAX_KEYS_PER_STEP = 4
MOUSE_LIMIT = 1000.0
SCROLL_LIMIT = 5.0

NOOP_ACTION_TEXT = f"{ACTION_START}0 0 0 ; ; ; ; ; ; {ACTION_END}"

# 模型训练用的 74 token 词表。大小写敏感：
# `one`..`nine` 是数字排键，`One`..`Twelve` 是 F1..F12。
LETTER_KEYS = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
DIGIT_KEYS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
FUNCTION_KEYS = ("One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
                 "Nine", "Ten", "Eleven", "Twelve")
MOUSE_BUTTON_KEYS = ("LB", "RB", "MB")
ARROW_KEYS = ("Up", "Down", "Left", "Right")
SPECIAL_KEYS = ("Shift", "Ctrl", "Alt", "Tab", "Caps", "Esc", "Space", "Enter", "Back",
                "Delete", "Insert", "Home", "End", "Pause")
PUNCTUATION_KEYS = ("Equal", "Minus", "Period", "Slash", "Quote")
KEY_VOCAB: frozenset[str] = frozenset(
    LETTER_KEYS + DIGIT_KEYS + FUNCTION_KEYS + MOUSE_BUTTON_KEYS + ARROW_KEYS
    + SPECIAL_KEYS + PUNCTUATION_KEYS
)

# `}` instead of `>` is a closing-bracket typo some models make.
_TAG_RE = re.compile(r"<\|action_start\|[>}](.*?)<\|action_end\|[>}]", re.DOTALL)
_BARE_RE = re.compile(r"^\s*-?\d")
# Lenient fallbacks (see `parse_action(lenient=True)`).
_WRAPPED_INT_RE = re.compile(
    r"<\|action(?:_start)?\|>\s*(-?\d+)\s+(-?\d+)\s+(-?\d+)(.*?)<\|action(?:_end)?\|>",
    re.DOTALL,
)
_SKELETON_RE = re.compile(r"[^\n;<]*(?:;[^\n;<]*){5,}")
_BARE_INT_RE = re.compile(r"(-?\d+)\s+(-?\d+)\s+(-?\d+)\s*(;[^\n<]*)", re.DOTALL)
_INT_RE = re.compile(r"^-?\d+$")


def split_reasoning(text: str) -> tuple[str, str, bool]:
    """``text`` -> (reasoning, reasoning 之后的文本, reasoning 是否闭合).

    未闭合的 ``<think>`` 意味着生成在动作出现前就结束了
    （token 预算用尽，或 stop 串在 reasoning 内命中），所以尾部为空。
    """
    text = text or ""
    if THINK_CLOSE in text:
        head, _, tail = text.rpartition(THINK_CLOSE)
        return head.split(THINK_OPEN)[-1].strip(), tail, True
    if THINK_OPEN in text:
        return text.split(THINK_OPEN)[-1].strip(), "", False
    return "", text, True


@dataclass
class Action:
    """一个解析后的 200ms 块。解析失败的文本是 no-op 且 ``parsed=False``。"""

    mouse: tuple[float, float, float] = (0.0, 0.0, 0.0)
    steps: list[list[str]] = field(default_factory=lambda: [[] for _ in range(CHUNK_STEPS)])
    parsed: bool = False
    truncated: bool = False
    body: str = ""
    dropped: list[str] = field(default_factory=list)

    @property
    def history_text(self) -> str:
        """这一轮在下一个提示词里作为 assistant 消息的样子。

        永远是格式完好的块：解析失败的轮次存为显式 no-op——
        把模型自己的失败放进上下文会教它重复失败。
        """
        if not self.parsed:
            return NOOP_ACTION_TEXT
        return f"{ACTION_START}{self.body}{ACTION_END}"

    @property
    def is_noop(self) -> bool:
        return not any(self.steps) and self.mouse == (0.0, 0.0, 0.0)

    @property
    def pressed(self) -> set[str]:
        return {k for group in self.steps for k in group}

    def to_text(self) -> str:
        x, y, z = (int(round(v)) for v in self.mouse)
        groups = " ; ".join(" ".join(g) for g in self.steps)
        return f"{ACTION_START}{x} {y} {z} ; {groups}{ACTION_END}"


def _build(nums: list[float], groups: list[str], body: str) -> Action:
    x = max(-MOUSE_LIMIT, min(MOUSE_LIMIT, nums[0]))
    y = max(-MOUSE_LIMIT, min(MOUSE_LIMIT, nums[1]))
    z = max(-SCROLL_LIMIT, min(SCROLL_LIMIT, nums[2]))
    steps: list[list[str]] = []
    dropped: list[str] = []
    for group in groups[:CHUNK_STEPS]:
        keys = []
        for tok in group.split():
            (keys if tok in KEY_VOCAB else dropped).append(tok)
        steps.append(keys[:MAX_KEYS_PER_STEP])
    while len(steps) < CHUNK_STEPS:
        steps.append([])
    return Action(mouse=(x, y, z), steps=steps, parsed=True, body=body, dropped=dropped)


def _parse_strict(tail: str) -> Action:
    m = _TAG_RE.search(tail)
    if m:
        body = m.group(1)
    elif _BARE_RE.match(tail) and ";" in tail:
        # 标签是特殊 token；剥离它们的服务器返回裸 body。
        body = tail.strip()
    else:
        return Action()
    parts = [seg.strip() for seg in body.split(";")]
    if not parts or not parts[0]:
        return Action()
    nums: list[float] = []
    for tok in parts[0].split()[:3]:
        try:
            nums.append(float(tok))
        except ValueError:
            return Action()
    if len(nums) < 3:
        return Action()
    return _build(nums, parts[1:], body)


def _parse_lenient(text: str) -> Action:
    """畸形回复的恢复（见 `parse_action`）。"""
    text = text.replace(THINK_OPEN, " ").replace(THINK_CLOSE, " ")
    m = _WRAPPED_INT_RE.search(text)
    if m:
        nums = [float(m.group(i)) for i in (1, 2, 3)]
        key_part = m.group(4).strip()
        body = text[m.start():m.end()]
        body = body[body.find(">") + 1:body.rfind("<")]
    else:
        skel = None
        for sm in _SKELETON_RE.finditer(text):
            skel = sm.group(0)
        if skel is None:
            bm = _BARE_INT_RE.search(text)
            if not bm:
                return Action()
            nums = [float(bm.group(i)) for i in (1, 2, 3)]
            key_part = bm.group(4).strip()
            body = bm.group(0)
        else:
            head = skel.partition(";")[0]
            head_tokens = head.split()
            if head_tokens and len(head_tokens) <= 3 and all(_INT_RE.match(t) for t in head_tokens):
                nums = [float(t) for t in head_tokens] + [0.0, 0.0, 0.0]
                key_part = skel[len(head):]
            else:
                nums = [0.0, 0.0, 0.0]
                key_part = skel
            body = skel
    segments = key_part.split(";") if key_part else []
    if segments and segments[0].strip() == "":
        segments = segments[1:]
    return _build(nums[:3], segments, body.strip())


def parse_action(text: str, *, lenient: bool = False) -> Action:
    """模型原始回复 -> :class:`Action`。永不抛异常；失败即 no-op。

    ``lenient=False``（默认）：只有最后一个 ``</think>`` 之后的块算数，
    带 tag 或裸 ``X Y Z ; ...`` body 均可。

    ``lenient=True``：额外恢复训练良好的 checkpoint 少见的形状——
    未闭合 ``<think>`` 里的动作、两数鼠标前缀、只有键的骨架——
    在回复中任意位置定位 `;` 分隔的六组骨架。
    """
    _, tail, closed = split_reasoning(text)
    if closed:
        action = _parse_strict(tail)
        if action.parsed or not lenient:
            return action
    elif not lenient:
        return Action(truncated=True)
    action = _parse_lenient(text or "")
    if not closed and not action.parsed:
        action.truncated = True
    return action
