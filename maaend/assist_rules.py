"""插件侧自包含的战斗归因规则引擎（四类失败原因）。

为什么有一份副本
----------------
算法源在 `maa_end_workspace/labeler/cline/{model,rules}.py`（打标 CLI 用）。
插件**不能** import 那个包：它在插件目录之外，跨目录 import 一搬家就坏
（`game_plugin_template/docs/范式.md` §4.2 的反面教材）。所以这里保留一份
**同构**实现，与 `assist.py` 同目录，随插件一起打包。

一致性保障：`maa_end_workspace/tests/test_assist_parity.py` 对同一批合成事件
逐条比对「本模块」与 `labeler/cline` 的标签、置信度、关键指标，防两份实现漂移。

依赖：仅标准库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# 事件拆包唯一实现在 domain/events.py（阶段 5 去重）。保留本模块的导出名，
# 因为调用方既有 `from .assist_rules import unwrap_events`，也有 assist 的 `_unwrap`。
from .domain.events import unwrap_events

# ---- 事件类型 ---------------------------------------------------------------
SCENE_INFO_TYPE = "SC_SELF_SCENE_INFO"
BATTLE_STATE_TYPE = "BattleOpModifyBattleState"
ENTITY_DIE_TYPE = "BattleOpEntityDie"
DAMAGE_TYPE = "damage"
TRIGGER_ACTION_TYPE = "BattleOpTriggerAction"
ADD_BUFF_TYPE = "BattleOpAddBuff"
FINISH_BUFF_TYPE = "BattleOpFinishBuff"
PASSIVE_TYPE = "BattleOpEnablePassiveSkill"
ENVELOPE_TYPES = ("hello", "event_batch")
DAMAGE_ACTION_TYPES = {"BattleActionDamage", 1, "1"}

# ---- 四类失败原因（对外契约标签）-------------------------------------------
LABEL_DPS = "dps_insufficient"
LABEL_SURVIVAL = "survivability_insufficient"
LABEL_ROTATION = "skill_rotation_error"
LABEL_CONFIG = "configuration_error"
LABEL_UNKNOWN = "unknown"

LABEL_ZH = {
    LABEL_DPS: "DPS不足",
    LABEL_SURVIVAL: "生存不足",
    LABEL_ROTATION: "技能循环错误",
    LABEL_CONFIG: "配置错误",
    LABEL_UNKNOWN: "未定",
}

#: 宏观建议话术（与 `labeler/advice.py` 的 MACRO_TEMPLATES 同措辞）
MACRO_BY_LABEL: dict[str, str] = {
    LABEL_DPS: "**DPS不足**：打了很久但没打死，队伍整体伤害跟不上。",
    LABEL_SURVIVAL: "**生存不足**：掉人太快，输出还没打出来就被清场了。",
    LABEL_ROTATION: "**技能循环错误**：技能放得太少或断档太久，循环没转起来。",
    LABEL_CONFIG: "**配置错误**：队伍本身没准备好，不是打得好不好的问题。",
    LABEL_UNKNOWN: "看不出明确的失败原因：证据不足，先不下结论。",
}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return default if value is None else int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


# 事件拆包唯一实现在 domain/events.py（阶段 5 去重）。保留本模块的导出名，
# 因为调用方既有 `from .assist_rules import unwrap_events`，也有 assist 的 `_unwrap`。


@dataclass(slots=True)
class PartyMember:
    battle_inst_id: int
    templateid: str | None = None
    display_name: str | None = None
    is_leader: bool = False
    squad_index: int = 0
    potential_level: int | None = None

    @property
    def name(self) -> str:
        return self.display_name or self.templateid or f"#{self.battle_inst_id}"


@dataclass(slots=True)
class BattleSummary:
    """一场战斗的聚合视图（字段名与 labeler 的 BattleRecord 对齐）。"""

    session_id: str
    started_at_ms: int
    party: dict[int, PartyMember] = field(default_factory=dict)
    ended_at_ms: int | None = None
    ended_by_state: bool = False
    damage_by_attacker: dict[int, float] = field(default_factory=dict)
    hits_by_attacker: dict[int, int] = field(default_factory=dict)
    crits_by_attacker: dict[int, int] = field(default_factory=dict)
    damage_timeline: list[tuple[int, float]] = field(default_factory=list)
    party_deaths: list[tuple[int, int]] = field(default_factory=list)
    min_hp_by_inst: dict[int, float] = field(default_factory=dict)
    enemy_deaths: int = 0
    enemy_min_hp: float | None = None
    skill_triggers: list[tuple[int, str | None]] = field(default_factory=list)
    passive_triggers: int = 0
    buffs_added: int = 0
    buffs_finished: int = 0
    event_count: int = 0
    raw_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_damage(self) -> float:
        return sum(self.damage_by_attacker.values())

    @property
    def duration_ms(self) -> int:
        """优先用战斗状态起止：上游按伤害时间戳算时长会让「零伤害速败」得 0。"""
        if self.ended_at_ms is not None and self.ended_at_ms >= self.started_at_ms:
            return self.ended_at_ms - self.started_at_ms
        if self.damage_timeline:
            return max(0, self.damage_timeline[-1][0] - self.damage_timeline[0][0])
        return 0

    @property
    def dps(self) -> float:
        seconds = self.duration_ms / 1000.0
        return self.total_damage / seconds if seconds > 0 else 0.0

    @property
    def party_size(self) -> int:
        return len(self.party)

    @property
    def died_inst_ids(self) -> set[int]:
        return {inst for inst, _ in self.party_deaths}

    @property
    def party_wipe(self) -> bool:
        return bool(self.party) and len(self.died_inst_ids) >= len(self.party)

    @property
    def damaged_party_members(self) -> int:
        return len({inst for inst in self.damage_by_attacker if inst in self.party})


def parse_squad(event: dict[str, Any]) -> list[PartyMember] | None:
    """从队伍事件取成员；非队伍事件返回 None。"""
    kind = event.get("type")
    if kind == "squad_update":
        raw = event.get("members")
        if not isinstance(raw, list):
            return None
        members: list[PartyMember] = []
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            inst = _as_int(item.get("battle_inst_id"))
            if not inst:
                continue
            members.append(PartyMember(
                battle_inst_id=inst,
                templateid=item.get("templateid"),
                display_name=item.get("display_name"),
                is_leader=bool(item.get("is_leader", False)),
                squad_index=_as_int(item.get("squad_index"), idx),
            ))
        return members
    if kind == SCENE_INFO_TYPE:
        raw = event.get("char_list")
        if not isinstance(raw, list):
            return []
        members = []
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            inst = _as_int(item.get("battle_inst_id"))
            if not inst:
                continue
            members.append(PartyMember(
                battle_inst_id=inst,
                templateid=item.get("templateid"),
                display_name=item.get("display_name"),
                is_leader=bool(item.get("is_leader", False)),
                squad_index=_as_int(item.get("squad_index"), idx),
                potential_level=(_as_int(item.get("potential_level"))
                                 if item.get("potential_level") is not None else None),
            ))
        return members
    return None


def extract_damage(event: dict[str, Any]) -> tuple[int | None, list[dict[str, Any]]] | None:
    """归一化伤害：`(attacker_inst_id, details)`；非伤害事件返回 None。"""
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    if kind == DAMAGE_TYPE:
        attacker = event.get("attacker")
        attacker_id = attacker.get("battle_inst_id") if isinstance(attacker, dict) else None
        details = event.get("details")
        if not isinstance(details, list):
            return None
        return (_as_int(attacker_id) if attacker_id is not None else None,
                [d for d in details if isinstance(d, dict)])
    if kind == TRIGGER_ACTION_TYPE:
        action = event.get("action")
        if not isinstance(action, dict):
            return None
        if action.get("action_type") not in DAMAGE_ACTION_TYPES:
            return None
        dmg_action = action.get("damage_action")
        if not isinstance(dmg_action, dict):
            return None
        details = dmg_action.get("details")
        if not isinstance(details, list):
            return None
        # 权威位置是 damage_action.attacker_id；容忍写在 action 层的变体
        attacker_id = dmg_action.get("attacker_id")
        if attacker_id is None:
            attacker_id = action.get("attacker_id")
        return (_as_int(attacker_id) if attacker_id is not None else None,
                [d for d in details if isinstance(d, dict)])
    return None


def _note_hp(record: BattleSummary, detail: dict[str, Any]) -> None:
    cur_hp = detail.get("cur_hp")
    if cur_hp is None:
        return
    hp = _as_float(cur_hp)
    target = _as_int(detail.get("target_battle_inst_id"), _as_int(detail.get("target_id")))
    if not target:
        return
    if target in record.party:
        prev = record.min_hp_by_inst.get(target)
        record.min_hp_by_inst[target] = hp if prev is None else min(prev, hp)
    else:
        prev = record.enemy_min_hp
        record.enemy_min_hp = hp if prev is None else min(prev, hp)


def split_battles(events: Iterable[dict[str, Any]]) -> list[BattleSummary]:
    """把事件流切成战斗记录（切分语义与 labeler 一致）。"""
    battles: list[BattleSummary] = []
    current: BattleSummary | None = None
    party: dict[int, PartyMember] = {}

    def close(record: BattleSummary | None, ended_at: int | None, by_state: bool) -> None:
        if record is None:
            return
        if ended_at is not None:
            record.ended_at_ms = ended_at
        record.ended_by_state = record.ended_by_state or by_state
        battles.append(record)

    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        ts = _as_int(event.get("timestamp_ms"))
        session_id = str(event.get("session_id") or "unknown")

        members = parse_squad(event)
        if members is not None:
            new_ids = sorted(m.battle_inst_id for m in members)
            if new_ids and new_ids != sorted(party):
                close(current, ts, False)
                current = None
            party = {m.battle_inst_id: m for m in members}
            if current is not None:
                current.party = dict(party)
            continue

        if kind == BATTLE_STATE_TYPE:
            if bool(event.get("is_in_battle", False)):
                if current is None:
                    current = BattleSummary(session_id=session_id, started_at_ms=ts, party=dict(party))
                current.raw_events.append(event)
            else:
                if current is not None:
                    current.raw_events.append(event)
                close(current, ts, True)
                current = None
            continue

        damage = extract_damage(event)
        if damage is not None:
            attacker_id, details = damage
            if current is None:
                current = BattleSummary(session_id=session_id, started_at_ms=ts, party=dict(party))
            current.raw_events.append(event)
            current.event_count += 1
            amount = 0.0
            hit = 0
            crit = 0
            for detail in details:
                amount += abs(_as_float(detail.get("abs_value"), _as_float(detail.get("value"))))
                hit += 1
                if detail.get("is_crit"):
                    crit += 1
                _note_hp(current, detail)
            if amount > 0 and attacker_id and attacker_id in current.party:
                current.damage_by_attacker[attacker_id] = (
                    current.damage_by_attacker.get(attacker_id, 0.0) + amount)
                current.hits_by_attacker[attacker_id] = current.hits_by_attacker.get(attacker_id, 0) + max(hit, 1)
                current.crits_by_attacker[attacker_id] = current.crits_by_attacker.get(attacker_id, 0) + crit
                current.damage_timeline.append((ts, amount))
            continue

        if current is None:
            continue
        current.raw_events.append(event)
        current.event_count += 1

        if kind == ENTITY_DIE_TYPE:
            inst = _as_int(event.get("entity_inst_id"))
            if not inst:
                continue
            if inst in current.party:
                current.party_deaths.append((inst, ts))
                current.min_hp_by_inst[inst] = 0.0
            else:
                current.enemy_deaths += 1
        elif kind == TRIGGER_ACTION_TYPE:
            current.skill_triggers.append((ts, event.get("template_str_id")))
        elif kind == ADD_BUFF_TYPE:
            current.buffs_added += 1
        elif kind == FINISH_BUFF_TYPE:
            current.buffs_finished += 1
        elif kind == PASSIVE_TYPE:
            current.passive_triggers += 1

    close(current, None, False)
    return battles


def battle_from_events(events: Iterable[dict[str, Any]]) -> list[BattleSummary]:
    """便捷入口：吃任意事件（含 WS 帧信封）→ 战斗记录。"""
    flat: list[dict[str, Any]] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ENVELOPE_TYPES:
            flat.extend(unwrap_events(item))
        else:
            flat.append(item)
    return split_battles(flat)


@dataclass(slots=True)
class RulesConfig:
    """规则阈值（与 labeler.cline.rules.RulesConfig 同默认值）。"""

    expected_party_size: int = 4
    long_battle_ms: int = 90_000
    fast_loss_ms: int = 20_000
    min_skill_per_minute: float = 4.0
    max_skill_gap_ratio: float = 0.45
    death_ratio_survival: float = 0.5
    early_death_ratio: float = 0.4
    min_total_damage: float = 0.0
    min_dps: float = 0.0
    damage_concentration: float = 0.75
    min_confidence: float = 0.35
    dominance_margin: float = 0.12
    #: 连续失败门槛（卡关信号 3）；插件侧由 UI Select 覆盖
    consecutive_fail_threshold: int = 3


@dataclass(slots=True)
class Evidence:
    code: str
    weight: float
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LabelResult:
    label: str
    label_zh: str
    confidence: float
    evidence: list[Evidence] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


def battle_metrics(record: BattleSummary, cfg: RulesConfig) -> dict[str, Any]:
    """把战斗记录压成规则指标（同时作为可解释输出）。"""
    duration_ms = record.duration_ms
    minutes = duration_ms / 60_000.0 if duration_ms > 0 else 0.0
    party_size = record.party_size
    deaths = len(record.party_deaths)
    first_death_ms = (record.party_deaths[0][1] - record.started_at_ms) if record.party_deaths else None
    skill_count = len(record.skill_triggers)
    gaps: list[int] = []
    if record.skill_triggers:
        stamps = sorted(ts for ts, _ in record.skill_triggers)
        # 头/尾空窗都要算：只看技能间隔会漏掉「打一半不放技能了」
        head = stamps[0] - record.started_at_ms
        if head > 0:
            gaps.append(head)
        gaps.extend(b - a for a, b in zip(stamps, stamps[1:]))
        tail = (record.ended_at_ms - stamps[-1]) if record.ended_at_ms is not None else 0
        if tail > 0:
            gaps.append(tail)
    max_gap = max(gaps) if gaps else (duration_ms if duration_ms else 0)
    total_hits = sum(record.hits_by_attacker.values())
    total_crits = sum(record.crits_by_attacker.values())
    top_damage = max(record.damage_by_attacker.values()) if record.damage_by_attacker else 0.0
    total_damage = record.total_damage
    return {
        "duration_ms": duration_ms,
        "duration_minutes": round(minutes, 4),
        "party_size": party_size,
        "expected_party_size": cfg.expected_party_size,
        "deaths": deaths,
        "death_ratio": round(deaths / party_size, 4) if party_size else 0.0,
        "first_death_ms": first_death_ms,
        "party_wipe": record.party_wipe,
        "total_damage": round(total_damage, 3),
        "dps": round(record.dps, 3),
        "enemy_deaths": record.enemy_deaths,
        "enemy_min_hp": record.enemy_min_hp,
        "skill_count": skill_count,
        "skill_per_minute": round(skill_count / minutes, 4) if minutes > 0 else 0.0,
        "max_skill_gap_ms": max_gap,
        "max_skill_gap_ratio": round(max_gap / duration_ms, 4) if duration_ms > 0 else 0.0,
        "buffs_added": record.buffs_added,
        "buffs_finished": record.buffs_finished,
        "passive_triggers": record.passive_triggers,
        "damaged_members": record.damaged_party_members,
        "silent_members": max(0, party_size - record.damaged_party_members),
        "crit_rate": round(total_crits / total_hits, 4) if total_hits else 0.0,
        "damage_concentration": round(top_damage / total_damage, 4) if total_damage > 0 else 0.0,
        "ended_by_state": record.ended_by_state,
        "event_count": record.event_count,
    }


def _score_dps(record: BattleSummary, m: dict[str, Any], cfg: RulesConfig) -> tuple[float, list[Evidence]]:
    ev: list[Evidence] = []
    score = 0.0
    if m["enemy_deaths"] == 0 and m["party_wipe"]:
        ev.append(Evidence("dps.enemy_not_cleared", 0.30,
                           "战斗结束时未击杀任何敌方单位且我方全灭", {"enemy_deaths": 0}))
        score += 0.30
    elif m["enemy_deaths"] == 0:
        ev.append(Evidence("dps.enemy_not_cleared", 0.20, "战斗结束时未击杀任何敌方单位",
                           {"enemy_deaths": 0}))
        score += 0.20
    if m["duration_ms"] >= cfg.long_battle_ms and m["enemy_deaths"] == 0:
        ev.append(Evidence("dps.long_uncleared", 0.22,
                           f"战斗持续 {m['duration_ms']}ms 仍未击杀敌方",
                           {"duration_ms": m["duration_ms"], "threshold_ms": cfg.long_battle_ms}))
        score += 0.22
    if cfg.min_total_damage > 0 and m["total_damage"] < cfg.min_total_damage:
        w = 0.30 * _clamp(1.0 - m["total_damage"] / cfg.min_total_damage)
        ev.append(Evidence("dps.total_damage_low", w, f"总伤害 {m['total_damage']} 低于门槛",
                           {"total_damage": m["total_damage"], "floor": cfg.min_total_damage}))
        score += w
    if cfg.min_dps > 0 and m["dps"] < cfg.min_dps:
        w = 0.30 * _clamp(1.0 - m["dps"] / cfg.min_dps)
        ev.append(Evidence("dps.dps_low", w, f"DPS {m['dps']} 低于门槛",
                           {"dps": m["dps"], "floor": cfg.min_dps}))
        score += w
    if m["party_size"] and m["silent_members"] > 0 and m["duration_ms"] >= cfg.fast_loss_ms:
        w = 0.18 * _clamp(m["silent_members"] / m["party_size"])
        ev.append(Evidence("dps.silent_members", w,
                           f"{m['silent_members']}/{m['party_size']} 名队员全程零伤害",
                           {"silent": m["silent_members"], "party_size": m["party_size"]}))
        score += w
    if m["total_damage"] <= 0 and m["duration_ms"] <= cfg.fast_loss_ms:
        ev.append(Evidence("dps.zero_damage_fast", 0.15, "速败且全程零伤害",
                           {"duration_ms": m["duration_ms"]}))
        score += 0.15
    return _clamp(score), ev


def _score_survival(record: BattleSummary, m: dict[str, Any], cfg: RulesConfig) -> tuple[float, list[Evidence]]:
    ev: list[Evidence] = []
    score = 0.0
    if m["party_wipe"]:
        ev.append(Evidence("survival.party_wipe", 0.50, "我方全员阵亡",
                           {"party_size": m["party_size"], "deaths": m["deaths"]}))
        score += 0.50
    elif m["deaths"] and m["death_ratio"] >= cfg.death_ratio_survival:
        w = 0.35 * _clamp(m["death_ratio"])
        ev.append(Evidence("survival.high_death_ratio", w,
                           f"阵亡 {m['deaths']}/{m['party_size']} 人（比例 {m['death_ratio']}）",
                           {"deaths": m["deaths"], "party_size": m["party_size"]}))
        score += w
    elif m["deaths"]:
        w = 0.15 * _clamp(m["death_ratio"] / max(cfg.death_ratio_survival, 1e-6))
        ev.append(Evidence("survival.some_deaths", w, f"出现阵亡 {m['deaths']} 人", {"deaths": m["deaths"]}))
        score += w
    if m["first_death_ms"] is not None and m["duration_ms"] > 0:
        ratio = m["first_death_ms"] / m["duration_ms"]
        if ratio <= cfg.early_death_ratio:
            w = 0.25 * _clamp(1.0 - ratio / max(cfg.early_death_ratio, 1e-6))
            ev.append(Evidence("survival.early_death", w,
                               f"首次阵亡发生在战斗进度 {ratio:.2f}（过早）",
                               {"first_death_ms": m["first_death_ms"], "duration_ms": m["duration_ms"]}))
            score += w
    if not m["deaths"] and record.min_hp_by_inst:
        near = sum(1 for hp in record.min_hp_by_inst.values() if 0 < hp <= 1)
        if near:
            w = 0.20 * _clamp(near / max(m["party_size"], 1))
            ev.append(Evidence("survival.near_death", w, f"{near} 名队员血量曾降至 1 以下",
                               {"near_death": near}))
            score += w
    return _clamp(score), ev


def _score_rotation(record: BattleSummary, m: dict[str, Any], cfg: RulesConfig) -> tuple[float, list[Evidence]]:
    ev: list[Evidence] = []
    score = 0.0
    if m["duration_ms"] < cfg.fast_loss_ms:
        return 0.0, ev
    if m["duration_minutes"] > 0 and m["skill_per_minute"] < cfg.min_skill_per_minute:
        w = 0.30 * _clamp(1.0 - m["skill_per_minute"] / max(cfg.min_skill_per_minute, 1e-6))
        ev.append(Evidence("rotation.low_skill_rate", w,
                           f"技能释放 {m['skill_per_minute']:.2f}/分钟，低于 {cfg.min_skill_per_minute}",
                           {"skill_per_minute": m["skill_per_minute"], "floor": cfg.min_skill_per_minute}))
        score += w
    if m["skill_count"] == 0:
        ev.append(Evidence("rotation.no_skill", 0.25, "整场战斗未观测到技能触发",
                           {"duration_ms": m["duration_ms"]}))
        score += 0.25
    if m["max_skill_gap_ratio"] > cfg.max_skill_gap_ratio:
        over = m["max_skill_gap_ratio"] - cfg.max_skill_gap_ratio
        w = 0.25 * _clamp(over / max(1.0 - cfg.max_skill_gap_ratio, 1e-6))
        ev.append(Evidence("rotation.skill_gap", w,
                           f"最长技能空窗占战斗 {m['max_skill_gap_ratio']:.2f}",
                           {"max_skill_gap_ms": m["max_skill_gap_ms"], "duration_ms": m["duration_ms"]}))
        score += w
    if m["buffs_finished"] > 0 and m["buffs_added"] == 0:
        ev.append(Evidence("rotation.buff_drop", 0.18,
                           f"{m['buffs_finished']} 个增益结束但无新增（未续接）",
                           {"finished": m["buffs_finished"]}))
        score += 0.18
    elif m["buffs_finished"] > m["buffs_added"] and m["buffs_finished"] >= 3:
        ev.append(Evidence("rotation.buff_decline", 0.12,
                           f"增益净减少（新增 {m['buffs_added']} / 结束 {m['buffs_finished']}）",
                           {"added": m["buffs_added"], "finished": m["buffs_finished"]}))
        score += 0.12
    if m["damage_concentration"] > cfg.damage_concentration and m["party_size"] >= 2:
        over = m["damage_concentration"] - cfg.damage_concentration
        w = 0.20 * _clamp(over / max(1.0 - cfg.damage_concentration, 1e-6))
        ev.append(Evidence("rotation.damage_concentration", w,
                           f"最高输出占比 {m['damage_concentration']:.2f}（输出过度集中）",
                           {"concentration": m["damage_concentration"]}))
        score += w
    # 可行性闸门：全灭时「没放技能」多半是死亡的后果而非原因
    if m["party_wipe"]:
        score *= 0.5
    return _clamp(score), ev


def _score_config(record: BattleSummary, m: dict[str, Any], cfg: RulesConfig) -> tuple[float, list[Evidence]]:
    ev: list[Evidence] = []
    score = 0.0
    if m["party_size"] == 0:
        ev.append(Evidence("config.empty_party", 0.60, "未解析到任何我方队员（队伍数据缺失）", {}))
        score += 0.60
    elif m["party_size"] < cfg.expected_party_size:
        missing = cfg.expected_party_size - m["party_size"]
        w = 0.35 * _clamp(missing / max(cfg.expected_party_size, 1))
        ev.append(Evidence("config.undersized_party", w,
                           f"队伍仅 {m['party_size']} 人（期望 {cfg.expected_party_size}）",
                           {"party_size": m["party_size"], "expected": cfg.expected_party_size}))
        score += w
    templates = [mem.templateid for mem in record.party.values() if mem.templateid]
    dupes = len(templates) - len(set(templates))
    if dupes > 0:
        w = 0.30 * _clamp(dupes / max(len(templates), 1))
        ev.append(Evidence("config.duplicate_members", w, f"队伍含 {dupes} 个重复角色模板",
                           {"duplicates": dupes}))
        score += w
    indices = sorted(mem.squad_index for mem in record.party.values())
    if indices and indices != list(range(len(indices))):
        ev.append(Evidence("config.squad_index_gap", 0.15, f"编队序号不连续：{indices}",
                           {"squad_index": indices}))
        score += 0.15
    if record.party and not any(mem.is_leader for mem in record.party.values()):
        ev.append(Evidence("config.no_leader", 0.12, "队伍中未标记队长", {}))
        score += 0.12
    if m["total_damage"] <= 0 and m["duration_ms"] <= cfg.fast_loss_ms:
        ev.append(Evidence("config.zero_damage_fast", 0.35,
                           "速败且零伤害：疑似配置/编队未生效或流程未进入战斗",
                           {"duration_ms": m["duration_ms"]}))
        score += 0.35
    return _clamp(score), ev


RULES = (
    (LABEL_DPS, _score_dps),
    (LABEL_SURVIVAL, _score_survival),
    (LABEL_ROTATION, _score_rotation),
    (LABEL_CONFIG, _score_config),
)


def classify(record: BattleSummary, cfg: RulesConfig | None = None) -> LabelResult:
    """对一场战斗做四类失败原因归因。"""
    cfg = cfg or RulesConfig()
    m = battle_metrics(record, cfg)
    scored: list[tuple[str, float, list[Evidence]]] = []
    for label, fn in RULES:
        score, ev = fn(record, m, cfg)
        scored.append((label, score, ev))
    scored.sort(key=lambda item: item[1], reverse=True)

    top_label, top_score, top_ev = scored[0]
    second_score = scored[1][1]
    alternatives = [
        {"label": lbl, "label_zh": LABEL_ZH[lbl], "confidence": round(sc, 4)}
        for lbl, sc, _ in scored[1:] if sc > 0
    ]
    if top_score < cfg.min_confidence:
        return LabelResult(
            label=LABEL_UNKNOWN, label_zh=LABEL_ZH[LABEL_UNKNOWN],
            confidence=round(top_score, 4), evidence=top_ev,
            alternatives=[{"label": lbl, "label_zh": LABEL_ZH[lbl], "confidence": round(sc, 4)}
                          for lbl, sc, _ in scored if sc > 0],
            metrics=m,
        )
    confidence = _clamp(0.5 + 0.5 * top_score)
    if second_score > 0 and (top_score - second_score) < cfg.dominance_margin:
        gap = max(0.0, top_score - second_score)
        confidence = _clamp(confidence * (0.6 + 0.4 * (gap / max(cfg.dominance_margin, 1e-6))))
    return LabelResult(label=top_label, label_zh=LABEL_ZH[top_label],
                       confidence=round(confidence, 4), evidence=top_ev,
                       alternatives=alternatives, metrics=m)


def classify_events(record: BattleSummary, cfg: RulesConfig | None = None) -> LabelResult:
    """`classify` 的别名（插件侧调用名）。"""
    return classify(record, cfg)


# =========================================================== 卡关检测（三信号）
# 与 labeler/cline/stuck.py 同构。三信号按需求给定优先级：
#   1 结算失败（派生复合判定，协议没有胜负字段）
#   2 全员阵亡
#   3 连续失败达标
# 参数由 UI Select 的 consecutive_fail_threshold 覆盖。

SIGNAL_SETTLEMENT = "settlement_failure"
SIGNAL_WIPE = "party_wipe"
SIGNAL_CONSECUTIVE = "consecutive_failures"
SIGNAL_NONE = "none"

SIGNAL_ZH = {
    SIGNAL_SETTLEMENT: "结算失败",
    SIGNAL_WIPE: "全员阵亡",
    SIGNAL_CONSECUTIVE: "连续失败",
    SIGNAL_NONE: "未卡关",
}

PRIORITY = (SIGNAL_SETTLEMENT, SIGNAL_WIPE, SIGNAL_CONSECUTIVE)


@dataclass(slots=True)
class StuckConfig:
    consecutive_fail_threshold: int = 3
    settlement_min_confidence: float = 0.6
    fast_loss_ms: int = 20_000
    #: 短于此值且零输出的「结束」更可能是掉线/退本，而不是真失败
    min_battle_ms_for_loss: int = 3_000


@dataclass(slots=True)
class StuckSignal:
    stuck: bool
    signal: str
    confidence: float
    detail: str
    consecutive_count: int = 0
    also: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stuck": self.stuck,
            "signal": self.signal,
            "signal_zh": SIGNAL_ZH.get(self.signal, self.signal),
            "confidence": round(self.confidence, 4),
            "detail": self.detail,
            "consecutive_count": self.consecutive_count,
            "also": [{"signal": s, "signal_zh": SIGNAL_ZH.get(s, s)} for s in self.also],
        }


def judge_settlement(record: BattleSummary, cfg: StuckConfig) -> tuple[bool, float, str]:
    """信号 1：派生「结算失败」（协议无胜负字段，故用可解释复合判据）。"""
    if record.ended_at_ms is None and not record.party_wipe:
        return False, 0.0, "战斗尚未结束，无法判定结算"

    duration = record.duration_ms or 0
    evidence = 0.0
    notes: list[str] = []
    if record.party_wipe:
        evidence += 0.55
        notes.append("我方全灭")
    if record.enemy_deaths == 0:
        evidence += 0.20
        notes.append("未击杀任何敌人")
    if record.total_damage <= 0:
        evidence += 0.15
        notes.append("零伤害")

    # 打够久却没清场：战斗**正常结束**而敌方一个没死，就是一次「没打过」。
    # 没有这条，「磨很久打不死人、但自己也没全灭」这种卡关（典型症状是技能循环
    # 失效）永远攒不起连续失败计数：信号 1 不够门槛、信号 2 无阵亡、
    # 信号 3 依赖的前置计数恒为 0，三层全部落空。
    if (record.enemy_deaths == 0 and record.ended_at_ms is not None
            and duration >= cfg.fast_loss_ms):
        evidence += 0.40
        notes.append(f"战斗持续 {duration}ms 后结束但未清场")

    confidence = min(0.98, evidence)
    if duration and duration < cfg.min_battle_ms_for_loss and record.total_damage <= 0:
        confidence *= 0.4
        notes.append(f"时长仅 {duration}ms 且零伤害，可能是掉线/退本而非失败")
    elif duration and duration <= cfg.fast_loss_ms and record.party_wipe:
        notes.append(f"速败（{duration}ms）")

    return confidence >= cfg.settlement_min_confidence, round(confidence, 4), "；".join(notes) or "无失败证据"


def judge_wipe(record: BattleSummary) -> tuple[bool, float, str]:
    """信号 2：全员阵亡（或死亡过半，算中等置信度的卡关压力）。"""
    size = record.party_size
    if size == 0:
        return False, 0.0, "无队伍数据"
    if record.party_wipe:
        return True, 0.95, f"全员阵亡（{len(record.party_deaths)}/{size}）"
    deaths = len(record.party_deaths)
    if deaths == 0:
        return False, 0.0, "无阵亡"
    ratio = deaths / size
    if ratio >= 0.5:
        return True, round(0.5 + 0.4 * ratio, 4), f"阵亡 {deaths}/{size}（过半）"
    return False, round(0.3 * ratio, 4), f"阵亡 {deaths}/{size}（未过半）"


def judge_consecutive(count: int, cfg: StuckConfig) -> tuple[bool, float, str]:
    """信号 3：连续失败次数达标。"""
    threshold = max(1, cfg.consecutive_fail_threshold)
    if count < threshold:
        return False, 0.0, f"连续失败 {count}/{threshold}"
    over = min(1.0, (count - threshold + 1) / max(threshold, 1))
    return True, round(0.6 + 0.3 * over, 4), f"连续失败 {count} 次（门槛 {threshold}）"


def detect_stuck(record: BattleSummary, consecutive_count: int,
                 cfg: StuckConfig | None = None) -> StuckSignal:
    """按优先级判定单场战斗是否卡关。"""
    cfg = cfg or StuckConfig()
    verdicts = {
        SIGNAL_SETTLEMENT: judge_settlement(record, cfg),
        SIGNAL_WIPE: judge_wipe(record),
        SIGNAL_CONSECUTIVE: judge_consecutive(consecutive_count, cfg),
    }
    for signal in PRIORITY:
        stuck, confidence, detail = verdicts[signal]
        if stuck:
            also = [s for s in PRIORITY if s != signal and verdicts[s][0]]
            return StuckSignal(True, signal, confidence, detail, consecutive_count, also)
    best = max(PRIORITY, key=lambda s: verdicts[s][1])
    _, confidence, detail = verdicts[best]
    return StuckSignal(False, SIGNAL_NONE, confidence, detail or "无卡关信号", consecutive_count)


def battle_failed(record: BattleSummary, cfg: StuckConfig | None = None) -> bool:
    """这场算不算「失败」——用于连续失败计数。

    与 labeler 的 scan_battles 同款顺序：先看结算，再看阵亡。
    """
    cfg = cfg or StuckConfig()
    failed, _, _ = judge_settlement(record, cfg)
    if not failed:
        failed, _, _ = judge_wipe(record)
    return failed


@dataclass(slots=True)
class FailureCounter:
    """连续失败计数（可持久化）。

    两个关键约束（都来自逆向结论，别「优化」掉）：
    1. `battle_inst_id` 会跨会话复用 → 用**事件指纹**去重：同一场战斗被 WS 与
       `logs/` 重复喂入时不能重复计数。
    2. WS 断线不补发 → 计数不能只活在内存里，要能持久化。

    `scope` 默认 `"global"`：终末地一次游玩会不断开新 `session_id`，
    **卡关的本质是「连续几把都没过」**，按会话分桶会让计数永远攒不起来。
    """

    threshold: int = 3
    scope: str = "global"
    sessions: dict[str, int] = field(default_factory=dict)
    last_fingerprint: dict[str, str] = field(default_factory=dict)

    @property
    def _bucket(self) -> str:
        return "__global__" if self.scope == "global" else "__session__"

    def fingerprint(self, record: BattleSummary) -> str:
        return (f"{record.session_id}|{record.started_at_ms}|"
                f"{record.ended_at_ms}|{record.event_count}")

    def observe(self, record: BattleSummary, failed: bool) -> int:
        """登记一场战斗结果，返回当前连续失败次数。"""
        session = record.session_id or "unknown"
        fp = self.fingerprint(record)
        if self.last_fingerprint.get(session) == fp:
            return self.count(session)  # 同一场重复输入 → 不重复计数
        self.last_fingerprint[session] = fp
        key = session if self.scope == "session" else self._bucket
        self.sessions[key] = (self.sessions.get(key, 0) + 1) if failed else 0
        return self.sessions[key]

    def count(self, session_id: str = "") -> int:
        key = (session_id or "unknown") if self.scope == "session" else self._bucket
        return self.sessions.get(key, 0)

    def as_dict(self) -> dict[str, Any]:
        return {"threshold": self.threshold, "scope": self.scope,
                "sessions": dict(self.sessions)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FailureCounter":
        return cls(
            threshold=int(payload.get("threshold") or 3),
            scope=str(payload.get("scope") or "global"),
            sessions={str(k): int(v) for k, v in (payload.get("sessions") or {}).items()},
        )