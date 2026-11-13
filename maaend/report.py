"""战斗复盘 —— 把 ZMDLogs 的 session 事件流汇总成对局报告（只读）。

数据流：find_logs_dir() → 最近 N 个 session_*.ndjson → 每行 _unwrap
       → split_battles → 逐场 battle_metrics / classify / detect_stuck
       → Markdown 报告文本。

与卡关辅助（assist.py）同源不同用：辅助是**实时**提醒（WS + logs 双源、
跨轮缓冲、到点推送）；复盘是**离线**总结（读全量 logs，产报告文本，
不订阅、不注入、不写文件）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .assist import find_logs_dir
from .assist_rules import (
    BattleSummary,
    RulesConfig,
    StuckConfig,
    battle_failed,
    battle_metrics,
    classify,
    detect_stuck,
    split_battles,
)
from .domain.events import unwrap_events

#: 单个 session 文件最多读取的字节数（从文件尾部读起；防超大文件拖垮内存）
MAX_READ_BYTES = 32 * 1024 * 1024
#: 每次复盘最多分析的战斗场数（保留最近）
MAX_BATTLES = 50

LEGEND = "「疑似失败」为派生的复合判据（协议无胜负字段），与卡关辅助同款语义。"


def list_session_files(logs_dir: Path, limit: int = 4) -> list[Path]:
    """按 mtime 取最新 N 个 session_*.ndjson。

    不按路径字典序——同一天目录里的 session UUID 不是时间序（assist 同款约定）。
    """
    try:
        files = [p for p in logs_dir.rglob("session_*.ndjson") if p.is_file()]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[: max(1, limit)]


def read_session_events(path: Path, max_bytes: int = MAX_READ_BYTES) -> list[dict[str, Any]]:
    """离线全量读一个 session ndjson → 事件列表（超限时从尾部截取）。"""
    events: list[dict[str, Any]] = []
    try:
        size = path.stat().st_size
    except OSError:
        return events
    start = max(0, size - max(1, max_bytes))
    try:
        with path.open("rb") as fh:
            fh.seek(start)
            if start:
                fh.readline()  # 截断点可能落在半行上，丢弃
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                events.extend(unwrap_events(payload))
    except OSError:
        return []
    return events


# ---------------------------------------------------------------- 格式化

def _fmt_duration(ms: float) -> str:
    if ms is None or ms <= 0:
        return "0s"
    sec = ms / 1000.0
    if sec < 60:
        return f"{sec:.1f}s"
    return f"{int(sec // 60)}m{int(sec % 60):02d}s"


def _fmt_damage(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if v >= 10_000:
        return f"{v / 1000.0:.1f}k"
    return f"{v:.0f}"


def _fmt_clock(ms: int | None) -> str:
    if not ms:
        return "?"
    try:
        return time.strftime("%H:%M:%S", time.localtime(ms / 1000.0))
    except (OverflowError, OSError, ValueError):
        return "?"


def _member_names(record: BattleSummary) -> str:
    names = [m.name for _, m in sorted(record.party.items())]
    return "、".join(names) if names else "（无队伍数据）"


def _top_contributor(record: BattleSummary) -> str:
    if not record.damage_by_attacker or record.total_damage <= 0:
        return "—"
    inst, dmg = max(record.damage_by_attacker.items(), key=lambda kv: kv[1])
    member = record.party.get(inst)
    name = member.name if member else f"#{inst}"
    return f"{name}（{dmg / record.total_damage:.0%}）"


# ---------------------------------------------------------------- 报告渲染

def build_report(
    battles: list[BattleSummary],
    *,
    source_label: str = "",
    now_ms: int | None = None,
    rules_cfg: RulesConfig | None = None,
    stuck_cfg: StuckConfig | None = None,
) -> str:
    """把战斗列表渲染成 Markdown 报告（纯函数，输入输出可测）。"""
    rules_cfg = rules_cfg or RulesConfig()
    stuck_cfg = stuck_cfg or StuckConfig()
    stamp = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime((now_ms if now_ms is not None else time.time() * 1000) / 1000.0),
    )

    lines: list[str] = ["# 终末地 · 战斗复盘", ""]
    lines.append(f"- 生成时间：{stamp}")
    if source_label:
        lines.append(f"- 数据源：{source_label}")
    lines.append(f"- 解析到战斗：{len(battles)} 场")
    lines.append(f"- 说明：{LEGEND}")
    lines.append("")
    if not battles:
        lines.append("> 没有解析到任何战斗数据（事件流里没有战斗起止标记）。")
        return "\n".join(lines) + "\n"

    # 逐场判定（连续失败按时间顺序累计，与卡关辅助同语义）
    rows: list[dict[str, Any]] = []
    streak = 0
    failed_n = 0
    for i, record in enumerate(battles, 1):
        failed = battle_failed(record, stuck_cfg)
        streak = streak + 1 if failed else 0
        if failed:
            failed_n += 1
        rows.append({
            "i": i,
            "record": record,
            "metrics": battle_metrics(record, rules_cfg),
            "failed": failed,
            "streak": streak,
            "stuck": detect_stuck(record, streak, stuck_cfg),
            "label": classify(record, rules_cfg),
        })

    lines.append(f"- 疑似失败：{failed_n} 场 / 共 {len(battles)} 场")
    lines.append("")

    # 一览表
    lines.append("## 一览")
    lines.append("")
    lines.append("| # | 开始 | 时长 | 伤害 | DPS | 阵亡 | 敌亡 | 判定 | 归因 |")
    lines.append("|---|------|------|------|-----|------|------|------|------|")
    for r in rows:
        rec, m = r["record"], r["metrics"]
        if rec.ended_at_ms is None:
            verdict = "⏳ 未结束"
        elif r["failed"]:
            verdict = "⛔ 疑似失败"
        else:
            verdict = "✅ 未见失败证据"
        attr = f'{r["label"].label_zh}（{r["label"].confidence:.0%}）' if r["failed"] else "—"
        lines.append(
            f'| {r["i"]} | {_fmt_clock(rec.started_at_ms)} | {_fmt_duration(m["duration_ms"])} '
            f'| {_fmt_damage(m["total_damage"])} | {m["dps"]:.0f} '
            f'| {m["deaths"]}/{m["party_size"]} | {m["enemy_deaths"]} | {verdict} | {attr} |'
        )
    lines.append("")

    # 逐场明细
    lines.append("## 逐场明细")
    lines.append("")
    for r in rows:
        rec, m = r["record"], r["metrics"]
        if rec.ended_at_ms is None:
            mark = "⏳ 未结束"
        elif r["failed"]:
            mark = "⛔ 疑似失败"
        else:
            mark = "✅ 未见失败证据"
        lines.append(f'### {r["i"]}. {_fmt_clock(rec.started_at_ms)} 起 · {_fmt_duration(m["duration_ms"])} · {mark}')
        lines.append("")
        lines.append(f'- 队伍：{_member_names(rec)}')
        lines.append(f'- 伤害：{_fmt_damage(m["total_damage"])}（DPS {m["dps"]:.0f}）；最高：{_top_contributor(rec)}')
        death_extra = f'（首个 {_fmt_clock(rec.party_deaths[0][1])}）' if rec.party_deaths else ""
        lines.append(
            f'- 阵亡 {m["deaths"]}/{m["party_size"]}{death_extra}；敌亡 {m["enemy_deaths"]}；'
            f'技能 {m["skill_count"]} 次（{m["skill_per_minute"]:.1f}/min，'
            f'最长空窗 {_fmt_duration(m["max_skill_gap_ms"])}）'
        )
        if r["failed"] or r["stuck"].stuck:
            sig = r["stuck"].as_dict()
            if sig["stuck"]:
                lines.append(
                    f'- 卡关信号：{sig["signal_zh"]}（置信 {sig["confidence"]:.0%}）——{sig["detail"]}'
                )
            lab = r["label"]
            ev = f'；证据：{lab.evidence[0].detail}' if lab.evidence else ""
            lines.append(f"- 归因：{lab.label_zh}（{lab.confidence:.0%}）{ev}")
            if lab.alternatives:
                alts = "；".join(f'{a["label_zh"]} {a["confidence"]:.0%}' for a in lab.alternatives[:2])
                lines.append(f"- 其他可能：{alts}")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 入口逻辑

def generate_report(
    *,
    log_dir: str = "",
    sessions: int = 1,
    max_battles: int = MAX_BATTLES,
    consecutive_fail_threshold: int = 3,
) -> dict[str, Any]:
    """读最近 N 个 session 文件 → 生成复盘报告。只读，不订阅、不写盘。

    返回 {ok, report, files, battles}；找不到日志目录时 ok=False + error。
    """
    root = find_logs_dir(log_dir)
    if root is None:
        return {
            "ok": False,
            "error": "未找到 ZMDLogs 的 logs 目录（未配置 assist_log_dir，常见位置也没有 session 文件）",
            "hint": "先启动 ZMDLogs（ef_zmdlogs_status action=start）或配置 assist_log_dir",
        }
    files = list_session_files(root, limit=max(1, int(sessions)))
    all_events: list[dict[str, Any]] = []
    for f in reversed(files):  # 旧 → 新拼接
        all_events.extend(read_session_events(f))
    battles = split_battles(all_events)
    if max_battles and len(battles) > max_battles:
        battles = battles[-max_battles:]  # 只保留最近

    stuck_cfg = StuckConfig(consecutive_fail_threshold=max(1, int(consecutive_fail_threshold)))
    report = build_report(
        battles,
        source_label=f"{root}（最近 {len(files)} 个 session 文件）",
        stuck_cfg=stuck_cfg,
    )
    return {
        "ok": True,
        "report": report,
        "files": [str(p) for p in files],
        "battles": len(battles),
    }


def report_stats(events: list[dict[str, Any]]) -> dict[str, Any]:
    """轻量统计（供测试与状态展示）：事件数 / 场数 / 失败数。"""
    battles = split_battles(events)
    failed = sum(1 for b in battles if battle_failed(b))
    return {"events": len(events), "battles": len(battles), "failed": failed}
