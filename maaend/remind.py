"""M4 人工辅助提醒（DESIGN-maaend-suite-modes.md §3 M4）：条件到点 → push_message。

会话内存态（插件重载即清，文档如实说明——不做磁盘持久化，v1 边界）。

三类提醒：
- after_task:  事件驱动——数据流里出现 task_succeeded / task_failed 即触发；
               value 填任务 entry（ef_list_tasks 可查，支持子串双向匹配），空 = 任意任务
- at_time:     每天 HH:MM（今天已过则顺延明天）
- in_minutes:  N 分钟后一次性

纯逻辑零依赖，tests/test_remind_logic.py 直测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

KINDS = ("after_task", "at_time", "in_minutes", "ocr_text")


@dataclass
class Reminder:
    id: str
    kind: str
    value: str
    message: str
    due_at: float = 0.0
    fired: bool = False
    fired_at: float = 0.0


def next_occurrence(hhmm: str, now: float | None = None) -> float:
    """每天 HH:MM 的下一次出现时间戳；今天已过则明天。非法格式抛 ValueError。"""
    text = str(hhmm or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"at_time 需 HH:MM 格式，收到 {text!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"at_time 需 HH:MM 格式，收到 {text!r}") from exc
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"at_time 时间越界：{text!r}")
    now = time.time() if now is None else now
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0,
                          lt.tm_wday, lt.tm_yday, -1))
    if target <= now:
        target += 86400
    return target


def _matches(filter_value: str, entry: str) -> bool:
    """after_task 的 value 与事件 entry 匹配：空 = 任意；否则子串双向匹配。"""
    if not filter_value:
        return True
    return filter_value in entry or entry in filter_value


class ReminderStore:
    """提醒的增删查 + 两类触发判定（事件 / 时间）。"""

    def __init__(self) -> None:
        self._items: list[Reminder] = []
        self._seq = 0

    # ------------------------------------------------------------ 增删查

    def add(self, kind: str, value: str, message: str) -> Reminder:
        kind = str(kind or "").strip()
        if kind not in KINDS:
            raise ValueError(f"未知提醒类型 {kind!r}；可用：{'、'.join(KINDS)}")
        value = str(value or "").strip()
        due = 0.0
        if kind == "in_minutes":
            try:
                minutes = int(value or "0")
            except ValueError as exc:
                raise ValueError("in_minutes 的 value 必须是分钟数") from exc
            if minutes < 1:
                raise ValueError("in_minutes 的 value 至少为 1 分钟")
            due = time.time() + minutes * 60
        elif kind == "at_time":
            due = next_occurrence(value)
        elif kind == "ocr_text":
            if not value:
                raise ValueError("ocr_text 的 value 必须是画面上要等待的文本（如『理智』恢复满的数字）")
        r = Reminder(id=f"r{self._seq + 1}", kind=kind, value=value,
                     message=str(message or ""), due_at=due)
        self._seq += 1
        self._items.append(r)
        return r

    def cancel(self, rid: str) -> bool:
        before = len(self._items)
        self._items = [r for r in self._items if r.id != str(rid or "")]
        return len(self._items) < before

    def pending(self) -> list[Reminder]:
        return [r for r in self._items if not r.fired]

    def to_dicts(self) -> list[dict]:
        return [{
            "id": r.id, "kind": r.kind, "value": r.value, "message": r.message,
            "due_at": round(r.due_at, 3) if r.due_at else None,
            "fired": r.fired,
            "fired_at": round(r.fired_at, 3) if r.fired_at else None,
        } for r in self._items]

    # ------------------------------------------------------------ 触发判定

    def observe(self, entries: list[dict]) -> list[tuple[Reminder, dict]]:
        """喂入数据流增量条目，返回被触发的 (reminder, 事件) 列表并标记 fired。"""
        fired: list[tuple[Reminder, dict]] = []
        for ev in entries or []:
            kind = str((ev or {}).get("kind") or "")
            if kind not in ("task_succeeded", "task_failed"):
                continue
            data = (ev or {}).get("data") or {}
            entry = str(data.get("entry") or "")
            for r in self._items:
                if r.fired or r.kind != "after_task":
                    continue
                if _matches(r.value, entry):
                    r.fired = True
                    r.fired_at = time.time()
                    fired.append((r, {"event_kind": kind, "entry": entry}))
        return fired

    def observe_ocr(self, full_text: str) -> list[Reminder]:
        """喂入一次 OCR 全文，返回画面出现目标文本而被触发的提醒（一次性）。"""
        fired = []
        text = str(full_text or "")
        for r in self._items:
            if r.fired or r.kind != "ocr_text" or not r.value:
                continue
            if r.value in text:
                r.fired = True
                r.fired_at = time.time()
                fired.append(r)
        return fired

    def due(self, now: float | None = None) -> list[Reminder]:
        """返回到点的时间类提醒并标记 fired。"""
        now = time.time() if now is None else now
        out = []
        for r in self._items:
            if r.fired or r.kind == "after_task" or r.due_at <= 0:
                continue
            if r.due_at <= now:
                r.fired = True
                r.fired_at = now
                out.append(r)
        return out

    def compose(self, r: Reminder, event: dict | None) -> str:
        """提醒文案（router 直接用，也可单测）。"""
        if r.kind == "after_task" and event:
            status = "成功" if event.get("event_kind") == "task_succeeded" else "失败"
            head = f"⏰ maa_end 提醒：任务 {event.get('entry') or '?'} 已{status}"
        elif r.kind == "at_time":
            head = f"⏰ maa_end 定时提醒（{r.value}）"
        elif r.kind == "ocr_text":
            head = f"⏰ maa_end 提醒：画面出现了「{r.value}」"
        else:
            head = "⏰ maa_end 提醒"
        return head + (f"：{r.message}" if r.message else "")
