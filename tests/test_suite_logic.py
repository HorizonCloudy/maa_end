#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""suite.py 纯逻辑自检：route_text / match_tasks / merge_ranking / nitrogen_playing。

跑法：python tests/test_suite_logic.py   （零依赖，不用 SDK）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "maaend"))

import suite  # noqa: E402

TASKS = [
    {"name": "Daily", "entry": "日常任务", "description": "基建收菜 线索 奖励领取"},
    {"name": "MatrixFilter", "entry": "基质筛选", "description": "识别基质词条 保留最优"},
    {"name": "SellProduct", "entry": "自动倒卖", "description": "跑商 购买 售卖"},
]


def main() -> None:
    # LINKS：三大家族链接齐全
    for key in ("MaaEnd 官网", "MAA 官网", "BetterGI 官网", "MaaFramework", "NitroGen 源码"):
        assert suite.LINKS[key].startswith("https://"), key
    assert suite.LINKS["MaaEnd 官网"] == "https://maaend.com/"

    # route_text：错误、空、正常三态
    assert "不可用" in suite.route_text({"error": "boom"})
    assert suite.route_text({}) == "决策层无返回"
    text = suite.route_text({"game": "终末地", "game_source": "alias", "intent": "qa",
                             "targets": [{"plugin": "maa_end", "entries": ["ef_decide"]}]})
    assert "终末地" in text and "ef_decide" in text

    # match_tasks：中文按字面命中、无命中为空
    hits = suite.match_tasks("帮我做终末地日常", TASKS, limit=3)
    assert hits and hits[0]["task"]["name"] == "Daily", hits
    assert all(h["score"] > 0 for h in hits)
    assert suite.match_tasks("完全无关的请求xyzzy", TASKS) == []

    # merge_ranking：决策概率优先、词面兜底、不混造数值
    ranked = suite.merge_ranking(
        TASKS,
        probabilities={"SellProduct": 0.9},
        keyword_hits=suite.match_tasks("帮我做终末地日常", TASKS, limit=3),
        limit=5,
    )
    assert ranked[0]["task"]["name"] == "SellProduct" and ranked[0]["source"] == "decision"
    assert any(r["task"]["name"] == "Daily" and r["source"] == "keyword" for r in ranked)
    # 无决策输入时纯词面排序也能出结果
    kw = suite.merge_ranking(TASKS, {}, suite.match_tasks("基质词条筛一下", TASKS), limit=3)
    assert kw and kw[0]["task"]["name"] == "MatrixFilter"

    # SuiteClient：错误一律 {"error": ...} 不抛（假 plugins 注入）
    class Boom:
        async def call_entry_json(self, *a, **k):
            raise RuntimeError("ipc down")

    class FakeResult:
        def __init__(self, value):
            self.value = value

    class OkPlugins:
        async def call_entry_json(self, ref, args, timeout=None):
            return FakeResult({"agent": {"running": True, "target_process": "endfield.exe"}})

    async def ipc_cases():
        boom = suite.SuiteClient(Boom())
        out = await boom.call("game_decision", "gd_route", {"query": "x"})
        assert out.get("error") and "ipc down" in out["error"], out
        ok = suite.SuiteClient(OkPlugins())
        st = ok.nitrogen_playing(await ok.nitrogen_status())
        assert st == {"known": True, "running": True,
                      "target_process": "endfield.exe"}, st
        assert ok.nitrogen_playing({"error": "nope"})["known"] is False

    asyncio.run(ipc_cases())
    print("test_suite_logic: ALL PASS")


if __name__ == "__main__":
    main()
