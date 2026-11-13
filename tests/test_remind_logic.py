#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""remind.py 纯逻辑自检：增删查 / 校验 / 事件触发 / 时间触发 / 文案。

跑法：python tests/test_remind_logic.py   （零依赖）
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "maaend"))

from remind import ReminderStore, next_occurrence  # noqa: E402


def test_add_validation() -> None:
    st = ReminderStore()
    r = st.add("in_minutes", "5", "叫我")
    assert r.id == "r1" and r.due_at > time.time() + 240
    assert st.add("after_task", "", "任意任务").kind == "after_task"
    r3 = st.add("at_time", "23:59", "睡前")
    assert r3.id == "r3" and r3.due_at > time.time()
    for bad in [(None, "x"), ("unknown", "x")]:
        try:
            st.add(*bad, "m")  # type: ignore[arg-type]
            raise AssertionError(f"应当拒绝 {bad}")
        except ValueError:
            pass
    for kind, value in (("in_minutes", "abc"), ("in_minutes", "0"), ("at_time", "25:00"),
                        ("at_time", "12:60"), ("at_time", "12")):
        try:
            st.add(kind, value, "m")
            raise AssertionError(f"应当拒绝 {kind}={value}")
        except ValueError:
            pass


def test_next_occurrence() -> None:
    base = time.mktime(time.strptime("2026-09-29 10:00:00", "%Y-%m-%d %H:%M:%S"))
    assert next_occurrence("10:30", base) == base + 1800       # 今天未过
    assert next_occurrence("09:30", base) == base + 86400 - 1800  # 今天已过 → 明天


def test_observe_matching() -> None:
    st = ReminderStore()
    st.add("after_task", "VisitFriendsMain", "好友做完了")
    st.add("after_task", "", "任何任务")
    st.add("after_task", "SellProduct", "倒卖")
    st.add("in_minutes", "30", "半小时后")  # 非事件类,observe 不触发
    ev_entry = {"kind": "task_succeeded", "data": {"entry": "VisitFriendsMain", "source": "m1"}}
    fired = st.observe([{"kind": "node", "data": {"node": "x"}}, ev_entry])
    entries = {r.value for r, _ in fired}
    assert entries == {"VisitFriendsMain", ""}   # 精确 + 任意 命中;子串未命中不误触
    # 已触发的不再触发
    assert st.observe([ev_entry]) == []
    # 子串双向匹配
    st2 = ReminderStore()
    st2.add("after_task", "Visit", "子串")
    assert st2.observe([ev_entry]) and st2.observe([]) == []


def test_due_and_cancel() -> None:
    st = ReminderStore()
    r = st.add("in_minutes", "1", "m")
    r.due_at = time.time() - 1  # 手动拨到已过期
    assert [x.id for x in st.due()] == [r.id]
    assert st.due() == []  # 一次性
    r2 = st.add("in_minutes", "60", "m2")
    assert st.cancel(r2.id) is True
    assert st.cancel(r2.id) is False
    assert st.cancel("nope") is False
    r2.due_at = 0
    assert st.due() == []  # 已取消的不触发
    assert len(st.pending()) == 0
    assert st.to_dicts()[0]["fired"] is True


def test_compose() -> None:
    st = ReminderStore()
    r = st.add("after_task", "X", "去看看")
    text = st.compose(r, {"event_kind": "task_succeeded", "entry": "XMain"})
    assert "XMain" in text and "成功" in text and "去看看" in text
    r2 = st.add("at_time", "12:00", "午休")
    assert "12:00" in st.compose(r2, None)
    r3 = st.add("in_minutes", "5", "")
    assert st.compose(r3, None).startswith("⏰")


def test_ocr_text_kind() -> None:
    st = ReminderStore()
    st.add("ocr_text", "135/135", "理智满了")
    st.add("in_minutes", "5", "")
    assert st.observe_ocr("SANITY 84/135 任务") == []  # 未出现
    fired = st.observe_ocr("理智 135/135 已恢复")
    assert len(fired) == 1 and fired[0].value == "135/135"
    assert st.observe_ocr("135/135") == []  # 一次性
    text = st.compose(fired[0], None)
    assert "135/135" in text and "理智满了" in text
    try:
        st.add("ocr_text", "", "x")
        raise AssertionError("空 value 应拒绝")
    except ValueError:
        pass


def main() -> None:
    test_add_validation()
    test_next_occurrence()
    test_observe_matching()
    test_due_and_cancel()
    test_compose()
    test_ocr_text_kind()
    print("test_remind_logic: ALL PASS")


if __name__ == "__main__":
    main()
