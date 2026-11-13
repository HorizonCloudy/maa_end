"""Endaxis 排轴数据码：3 套预设（开荒 / 精英 / BOSS）+ 编解码。

格式对齐 MaaEnd v2.29.0 agent/go-service/autofight/endaxistimeline.go：
- 数据码 = base64url(gzip(JSON))，去掉尾随 '='；
- JSON 只被读 scenarioList[].{id,name,data.tracks[].{id,actions[].{type,startTime,duration,name,id}}}；
- 时间单位为帧，60fps，战斗 t=0 对应 startTime=300；
- 只派发 type=ultimate / skill（battleSkill 等价 skill），track 下标 0..3 = 队伍 1..4 号位；
- 按 scenarioList 顺序选第一个可用场景：含终结技的 track 对应角色终结技未满就跳过该场景，
  所以终结技爆发场景放前面，纯战技轮转放最后兜底。

预设是起手模板，没在实机上调过；网站侧的完整导出字段（damageTicks 等）这里不生成，
因此数据码只保证 MaaEnd 能解析，不保证能导回 end-axis.com 编辑。
"""

from __future__ import annotations

import base64
import gzip
import json

FRAME_BASE = 300
FPS = 60

#: (动作类型, 号位 0..3, 开始秒, 持续秒)
_S, _U = "skill", "ultimate"

#: name → (说明, 跳过连携冷却等待, [(场景名, [动作...]), ...])
PRESETS: dict[str, tuple[str, bool, list[tuple[str, list[tuple[str, int, float, float]]]]]] = {
    "开荒": (
        "杂兵清图：终结技满就全员放，否则 3 人战技每 4 秒轮转；不等连携冷却，循环快。",
        True,
        [
            ("全员终结技清场", [(_U, 0, 0.0, 2.0), (_U, 1, 0.5, 2.0), (_U, 2, 1.0, 2.0), (_U, 3, 1.5, 2.0)]),
            ("战技轮转", [(_S, 0, 0.0, 1.0), (_S, 1, 4.0, 1.0), (_S, 2, 8.0, 1.0)]),
        ],
    ),
    "精英": (
        "精英怪：辅助战技铺垫后主 C（1 号位）终结技 + 战技；终结技没满退回战技轮转。",
        True,
        [
            ("主C终结技", [(_S, 1, 0.0, 1.0), (_S, 2, 1.0, 1.0), (_U, 0, 2.0, 2.0), (_S, 0, 4.0, 1.0)]),
            ("战技轮转", [(_S, 1, 0.0, 1.0), (_S, 2, 4.0, 1.0), (_S, 0, 8.0, 1.0)]),
        ],
    ),
    "BOSS": (
        "首领：等全员连携就绪再开轴，辅助战技 → 辅助终结技 → 主 C 终结技 + 战技集中爆发；"
        "条件不满时依次退到主 C 单终结技、战技轮转。",
        False,
        [
            ("全员爆发", [(_S, 1, 0.0, 1.0), (_S, 2, 0.8, 1.0), (_U, 3, 1.6, 2.0), (_U, 2, 2.4, 2.0),
                          (_U, 1, 3.2, 2.0), (_U, 0, 4.0, 2.0), (_S, 0, 6.0, 1.0)]),
            ("主C终结技", [(_U, 0, 0.0, 2.0), (_S, 0, 2.0, 1.0)]),
            ("战技轮转", [(_S, 1, 0.0, 1.0), (_S, 2, 4.0, 1.0), (_S, 0, 8.0, 1.0)]),
        ],
    ),
}


def build_project(preset: str, members: int = 4) -> dict:
    """生成项目 JSON。members<4 时丢掉超出号位的动作：MaaEnd 派发不看队伍人数，
    空号位上的动作会按到不存在的角色上。丢空的场景整条去掉。"""
    if preset not in PRESETS:
        raise ValueError(f"没有预设 {preset!r}：可选 {'、'.join(PRESETS)}")
    if not 1 <= int(members) <= 4:
        raise ValueError("members 必须是 1..4")
    members = int(members)
    scenarios = []
    for si, (sc_name, actions) in enumerate(PRESETS[preset][2]):
        tracks = [{"id": f"track{i + 1}", "actions": []} for i in range(members)]
        for ai, (kind, slot, start, dur) in enumerate(actions):
            if slot >= members:
                continue
            tracks[slot]["actions"].append({
                "type": kind,
                "startTime": FRAME_BASE + round(start * FPS),
                "duration": round(dur * FPS),
                "name": f"{slot + 1}号位{'终结技' if kind == _U else '战技'}",
                "id": f"s{si}a{ai}",
            })
        if any(t["actions"] for t in tracks):
            scenarios.append({"id": f"s{si}", "name": sc_name, "data": {"tracks": tracks}})
    return {"scenarioList": scenarios}


def encode(project: dict) -> str:
    raw = json.dumps(project, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(gzip.compress(raw, mtime=0)).decode("ascii").rstrip("=")


def decode(code: str) -> dict:
    text = code.strip().rstrip("=")
    return json.loads(gzip.decompress(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))))


def run_options(code: str, skip_combo_cooldown: bool) -> dict:
    """RealTimeTask 的选项链：AutoFight → 完整设置 → 排轴 → 数据码。"""
    return {
        "AutoFight": "Yes",
        "AutoFightSettingFull": "Yes",
        "AutoFightAxisFullSetting": "Yes",
        "AutoFightAxisData": {"AutoFightAxisCodeString": code},
        "AutoFightAxisSkipComboCooldown": "Yes" if skip_combo_cooldown else "No",
    }

def summarize(project: dict) -> dict:
    """统计一个轴项目：场景数、动作数、按类型/号位分布。"""
    scenarios = (project or {}).get("scenarioList") or []
    by_type: dict[str, int] = {}
    by_slot: dict[int, int] = {}
    total = 0
    for sc in scenarios:
        tracks = ((sc.get("data") or {}).get("tracks") or [])
        for ti, tr in enumerate(tracks):
            for a in (tr.get("actions") or []):
                total += 1
                t = str(a.get("type") or "")
                by_type[t] = by_type.get(t, 0) + 1
                by_slot[ti] = by_slot.get(ti, 0) + 1
    return {"scenarios": len(scenarios), "actions": total,
            "by_type": by_type, "by_slot": {str(k): v for k, v in sorted(by_slot.items())}}


def compare(code: str, preset: str, members: int = 4) -> dict:
    """解社区分享的轴数据码，与本地预设做结构对比。

    **只读不写**：社区轴仅解析统计，不执行、也不改写成 MaaEnd 能用的格式
    （网站侧的 damageTicks 等字段本插件不生成，见模块 docstring）。
    这样「对照」不会因为字段缺失而产出半残的轴。

    失败情形：码不是 `base64url(gzip(JSON))`（比如粘了网页链接、或换了格式）。
    """
    if not code or not str(code).strip():
        raise ValueError("compare 需要 code（社区分享的轴数据码）")
    try:
        community = decode(str(code))
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都归为格式不符
        raise ValueError(
            "数据码解析失败：不是 base64url+gzip 的 JSON。"
            f"原始错误 {type(exc).__name__}: {str(exc)[:80]}"
        ) from exc
    mine = build_project(preset, members)
    a, b = summarize(community), summarize(mine)
    return {
        "community": a,
        "preset": {"name": preset, **b},
        "diff": {
            "actions": a["actions"] - b["actions"],
            "scenarios": a["scenarios"] - b["scenarios"],
            "ultimate": a["by_type"].get("ultimate", 0) - b["by_type"].get("ultimate", 0),
        },
        "note": "社区轴仅解析对比，不执行、不改写成 MaaEnd 格式",
    }
