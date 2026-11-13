#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report.py 纯逻辑自检：合成会话 → 复盘报告（渲染 / 读盘管道 / 边界）。

跑法：python tests/test_report_logic.py   （零第三方依赖，不碰真实 logs）

覆盖：
1. build_report 对「胜利 + 生存失败」两场的 Markdown 渲染（判定 / 归因 / 表格）；
2. generate_report 完整读盘管道（临时 logs 目录里的 session_*.ndjson → 报告）；
3. 空目录边界（ok=True + 空报告，不误报）；
4. report_stats 轻量统计。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maaend.report as report  # noqa: E402
import maaend.synthetic_events as synthetic  # noqa: E402
from maaend.assist_rules import split_battles  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), label, detail))
    line = "[%s] %s" % ("PASS" if ok else "FAIL", label)
    if detail:
        line += "  -> " + detail
    print(line, flush=True)


def _two_battle_events() -> list[dict]:
    """胜利 + 生存失败，两场独立会话。"""
    win = synthetic.generate_sessions(scenario="victory", count=1, seed=5)[0]
    lose = synthetic.generate_sessions(scenario="survivability_insufficient",
                                       count=1, seed=2)[0]
    return win + lose


def _write_session(path: Path, events: list[dict], sid: str) -> None:
    payload = {"type": "event_batch", "session_id": sid, "events": events}
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def test_build_report_two_battles() -> None:
    events = _two_battle_events()
    battles = split_battles(events)
    check(len(battles) == 2, "切分出 2 场战斗", f"battles={len(battles)}")

    md = report.build_report(battles, source_label="（合成测试）",
                             now_ms=1_780_000_000_000)
    check("解析到战斗：2 场" in md, "报告头部统计正确")
    check("⛔ 疑似失败" in md and "✅ 未见失败证据" in md,
          "胜负判定渲染（1 失败 + 1 通场）",
          f"failed_mark={'⛔ 疑似失败' in md}, pass_mark={'✅ 未见失败证据' in md}")
    check("生存不足" in md, "失败归因渲染（期望「生存不足」）")
    check("| # | 开始 | 时长 | 伤害 | DPS |" in md, "一览表头渲染")
    check("### 1." in md and "### 2." in md, "逐场明细渲染")
    check("- 队伍：" in md and "- 伤害：" in md, "明细字段（队伍/伤害）")


def test_generate_report_from_logs_dir() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="report-test-"))
    win = synthetic.generate_sessions(scenario="victory", count=1, seed=5)[0]
    lose = synthetic.generate_sessions(scenario="survivability_insufficient",
                                       count=1, seed=2)[0]
    _write_session(tmp / "session_a.ndjson", win, "sess-a")
    _write_session(tmp / "session_b.ndjson", lose, "sess-b")

    res = report.generate_report(log_dir=str(tmp), sessions=2)
    check(res.get("ok") is True, "读盘管道 ok", str(res.get("error", "")))
    check(res.get("battles") == 2, "读盘管道切分 2 场", f"battles={res.get('battles')}")
    check(len(res.get("files") or []) == 2, "列出 2 个 session 文件")
    md = res.get("report") or ""
    check("解析到战斗：2 场" in md and "疑似失败：1 场" in md,
          "报告统计（2 场 / 失败 1 场）",
          f"head_ok={'解析到战斗：2 场' in md}")


def test_generate_report_empty_dir() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="report-empty-"))
    res = report.generate_report(log_dir=str(tmp), sessions=2)
    md = res.get("report") or ""
    check(res.get("ok") is True and res.get("battles") == 0,
          "空目录：ok=True 且 0 场（不误报）",
          f"battles={res.get('battles')}")
    check("没有解析到任何战斗数据" in md, "空目录：报告如实说明")


def test_report_stats() -> None:
    st = report.report_stats(_two_battle_events())
    check(st["battles"] == 2 and st["failed"] >= 1,
          "report_stats 统计", f"{st}")


def main() -> int:
    tests = [
        test_build_report_two_battles,
        test_generate_report_from_logs_dir,
        test_generate_report_empty_dir,
        test_report_stats,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - 单测异常按 FAIL 记录
            import traceback
            check(False, f"{fn.__name__} 异常", f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
    failed = sum(1 for r in RESULTS if not r[0])
    print()
    print("共 %d 项，失败 %d 项" % (len(RESULTS), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
