"""入口契约快照 —— 重构期的第一道护栏。

全盘重构（docs/REFACTOR-PLAN.md）期间 `router.py` 会被拆成多个 mixin，
最容易出的事故是「某个 @plugin_entry 静默丢失」或「timeout 被改掉」——
两者都不会让测试变红，只会让用户在运行时发现入口不见了。

所以把当前入口面固化成快照：
  · 25 个 plugin_entry（含 status 这份 ui.action 同体入口）
  · 每个 entry 的 timeout 秒数
  · 22 个 llm_tool 名称

改动入口是允许的，但必须同步改这个文件 —— 让 diff 出现在 code review 里，
而不是在生产环境里。

跑法：PYTHONPATH="<plugins 父目录>;<宿主 SDK 仓库>" python -m pytest tests/test_entry_contract.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 插件根的父目录：让 maa_end 包可导入

from maa_end.maaend.router import MaaEndRouter  # noqa: E402
from plugin.sdk.plugin.llm_tool import collect_llm_tool_methods  # noqa: E402

#: entry id → timeout（秒）。顺序按 id 排序，便于 diff。
EXPECTED_ENTRIES: dict[str, float] = {
    "ef_adb": 30.0,
    "ef_agent": 60.0,
    "ef_assist": 20.0,
    "ef_decide": 120.0,
    "ef_endaxis": 120.0,
    "ef_gamescaling": 60.0,
    "ef_kb": 60.0,
    "ef_list_tasks": 60.0,
    "ef_modes": 60.0,
    "ef_ocr": 60.0,
    "ef_open_app": 30.0,
    "ef_remind": 15.0,
    "ef_reminders": 15.0,
    "ef_replay": 90.0,
    "ef_report": 120.0,
    "ef_run_task": 120.0,
    "ef_scene_status": 30.0,
    "ef_status": 60.0,
    "ef_stop_task": 30.0,
    "ef_stream": 30.0,
    "ef_suite": 60.0,
    "ef_task_state": 30.0,
    "ef_yolo": 60.0,
    "ef_zmdlogs_status": 30.0,
    "status": 15.0,
}

#: llm_tool 名称集合（顺序无关）。
EXPECTED_TOOLS: frozenset[str] = frozenset({
    "maa_adb",
    "maa_agent",
    "maa_assist_advice",
    "maa_daily",
    "maa_decide",
    "maa_endfield_lookup",
    "maa_gamescaling",
    "maa_list_tasks",
    "maa_ocr",
    "maa_ocr_find",
    "maa_ocr_read",
    "maa_pyenv_probe",
    "maa_remind",
    "maa_run_task",
    "maa_scene",
    "maa_scene_expect",
    "maa_status",
    "maa_stop_task",
    "maa_stream_read",
    "maa_task_state",
    "maa_yolo_detect",
    "maa_zmdlogs",
})


def _router() -> MaaEndRouter:
    """构造即完成装饰器扫描（不绑定主插件、不碰游戏窗口）。"""
    return MaaEndRouter()


def test_entry_ids_unchanged() -> None:
    got = set(_router().collect_entries())
    missing = sorted(set(EXPECTED_ENTRIES) - got)
    added = sorted(got - set(EXPECTED_ENTRIES))
    assert not missing, f"入口丢失：{missing}"
    assert not added, f"入口新增（请同步快照）：{added}"


def test_entry_timeouts_unchanged() -> None:
    entries = _router().collect_entries()
    drift = {
        entry_id: (EXPECTED_ENTRIES[entry_id], getattr(entries[entry_id].meta, "timeout", None))
        for entry_id in EXPECTED_ENTRIES
        if entry_id in entries and getattr(entries[entry_id].meta, "timeout", None) != EXPECTED_ENTRIES[entry_id]
    }
    assert not drift, f"timeout 变更（旧, 新）：{drift}"


def test_llm_tool_names_unchanged() -> None:
    got = {meta.name for meta, _bound in collect_llm_tool_methods(_router())}
    missing = sorted(EXPECTED_TOOLS - got)
    added = sorted(got - EXPECTED_TOOLS)
    assert not missing, f"llm_tool 丢失：{missing}"
    assert not added, f"llm_tool 新增（请同步快照）：{added}"


def test_every_entry_accepts_kwargs() -> None:
    """宿主注入 `_ctx` 时会带额外 kwargs；入口签名必须收 `**_`。"""
    import inspect

    router = _router()
    bad: list[str] = []
    for entry_id, record in router.collect_entries().items():
        handler = record.handler
        try:
            sig = inspect.signature(handler)
        except (TypeError, ValueError):
            continue
        if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            bad.append(entry_id)
    assert not bad, f"这些入口不收 **kwargs，宿主注入会炸：{bad}"
