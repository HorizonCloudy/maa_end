"""合成战斗事件生成器（插件侧自检 / 演示用）。

为什么插件里要带这个
--------------------
1. **自检**：`tests/maaend_stub_run.py` 与设置面板的「试一下」需要一批**已知答案**的
   事件来验证归因链路，而不能要求用户先装 ZMDLogs、先打一把。
2. **演示**：可以立刻展示分层输出效果（宏观 vs 精细）。

事件字段与 ZMDLogs 的 `event_batch` 完全同构（见 docs/ws-reverse-engineering.md），
所以这批数据也能直接喂给打标 CLI，两边共用同一套语义。

⚠️ 这些样本是**合成的**（`synthetic`），不得冒充真实战斗数据。

依赖：仅标准库（`random`）。
"""

from __future__ import annotations

import copy
import random
from typing import Any

# 与 labeler/cline/synthetic.py 同值：T0 一致便于两边比对
SESSION = "synthetic"
T0 = 1_800_000_000_000

PARTY: list[dict[str, Any]] = [
    {"battle_inst_id": 101, "templateid": "chr_0001", "display_name": "队长",
     "is_leader": True, "squad_index": 0},
    {"battle_inst_id": 102, "templateid": "chr_0002", "display_name": "输出甲",
     "is_leader": False, "squad_index": 1},
    {"battle_inst_id": 103, "templateid": "chr_0003", "display_name": "输出乙",
     "is_leader": False, "squad_index": 2},
    {"battle_inst_id": 104, "templateid": "chr_0004", "display_name": "辅助",
     "is_leader": False, "squad_index": 3},
]


def _squad_event(ts: int, party: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"type": "squad_update", "session_id": SESSION, "timestamp_ms": ts,
            "members": party if party is not None else PARTY}


def _scene_event(ts: int, party: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    members = party if party is not None else PARTY
    return {
        "type": "SC_SELF_SCENE_INFO", "session_id": SESSION, "timestamp_ms": ts,
        "char_list": [
            {"id": 900 + i, "templateid": m["templateid"],
             "battle_inst_id": m["battle_inst_id"], "display_name": m["display_name"],
             "potential_level": 0, "is_leader": m["is_leader"], "squad_index": m["squad_index"]}
            for i, m in enumerate(members)
        ],
    }


def _battle_state(ts: int, in_battle: bool) -> dict[str, Any]:
    return {"type": "BattleOpModifyBattleState", "session_id": SESSION,
            "timestamp_ms": ts, "seq_id": None, "client_tick_tms": None,
            "is_in_battle": in_battle}


def _damage(ts: int, attacker: int, target: int, amount: float,
            hp: float | None, crit: bool = False) -> dict[str, Any]:
    return {
        "type": "damage", "session_id": SESSION, "timestamp_ms": ts,
        "seq_id": None, "client_tick_tms": None, "skill_template_id": None,
        "attacker": {"battle_inst_id": attacker, "display_name": None},
        "target": {"battle_inst_id": target, "display_name": None},
        "details": [{"target_battle_inst_id": target, "is_crit": crit,
                     "value": amount, "abs_value": amount, "cur_hp": hp}],
    }


def _trigger_action(ts: int, inst: int, template: str, attacker: int, target: int,
                    amount: float, hp: float | None) -> dict[str, Any]:
    """伤害型动作。

    字段位置严格对齐上游 `record_parser._extract_damage_event`：
    `action.damage_action.attacker_id` —— `attacker_id` 在 **damage_action 内部**，
    不在 action 层（这与 `damage` 事件的字段名不同）。
    """
    return {
        "type": "BattleOpTriggerAction", "session_id": SESSION, "timestamp_ms": ts,
        "seq_id": None, "client_tick_tms": None, "owner_id": None, "owner_type": None,
        "inst_id": inst, "template_type": "skill", "template_int_id": 5001,
        "template_str_id": template, "level": 1,
        "action": {
            "action_type": "BattleActionDamage",
            "damage_action": {
                "attacker_id": attacker,
                "details": [{"target_id": target, "is_crit": False, "value": amount,
                             "abs_value": amount, "cur_hp": hp}],
            },
        },
    }


def _skill_event(ts: int, inst: int, template: str) -> dict[str, Any]:
    """非伤害的技能触发（技能循环统计用）。"""
    return {"type": "BattleOpTriggerAction", "session_id": SESSION, "timestamp_ms": ts,
            "seq_id": None, "client_tick_tms": None, "inst_id": inst,
            "template_type": "skill", "template_int_id": 5002,
            "template_str_id": template, "level": 1,
            "action": {"action_type": "BattleActionSkill"}}


def _add_buff(ts: int, buff_id: int, src: int, target: int, str_id: str) -> dict[str, Any]:
    return {"type": "BattleOpAddBuff", "session_id": SESSION, "timestamp_ms": ts,
            "seq_id": None, "client_tick_tms": None, "int_id": None, "str_id": str_id,
            "buff_inst_id": buff_id, "src_inst_id": src, "target_inst_id": target,
            "assigned_items": {}}


def _finish_buff(ts: int, buff_id: int) -> dict[str, Any]:
    return {"type": "BattleOpFinishBuff", "session_id": SESSION, "timestamp_ms": ts,
            "seq_id": None, "client_tick_tms": None, "buff_inst_id": buff_id}


def _passive(ts: int, skill_inst: int) -> dict[str, Any]:
    return {"type": "BattleOpEnablePassiveSkill", "session_id": SESSION,
            "timestamp_ms": ts, "seq_id": None, "client_tick_tms": None,
            "skill_inst_id": skill_inst}


def _entity_die(ts: int, inst: int) -> dict[str, Any]:
    return {"type": "BattleOpEntityDie", "session_id": SESSION, "timestamp_ms": ts,
            "seq_id": None, "client_tick_tms": None, "entity_inst_id": inst}


def scenario_dps_insufficient(seed: int = 1) -> list[dict[str, Any]]:
    """DPS不足：打得久、敌人不死、我方慢慢被磨死、技能照常放。"""
    rng = random.Random(seed)
    ev: list[dict[str, Any]] = [_squad_event(T0), _scene_event(T0 + 50),
                               _battle_state(T0 + 100, True)]
    duration = 150_000
    ev.append(_add_buff(T0 + 200, 1, 104, 102, "atk_up"))
    for i in range(0, duration, 5_000):
        ts = T0 + 1_000 + i
        ev.append(_skill_event(ts, 101, f"skill_{i}"))       # 技能循环正常
        ev.append(_damage(ts + 200, 102, 9001, 1_200 + rng.random() * 300, 900_000 - i * 500))
        ev.append(_damage(ts + 400, 103, 9001, 900 + rng.random() * 200, 880_000 - i * 400))
        if i >= 100_000:                                     # 后期开始减员
            ev.append(_damage(ts + 600, 9001, 104, 4_000,
                              max(1.0, 30_000 - (i - 100_000) * 30)))
    ev.append(_entity_die(T0 + 120_000, 104))
    ev.append(_entity_die(T0 + 140_000, 103))
    ev.append(_battle_state(T0 + duration, False))
    return ev


def scenario_survivability_insufficient(seed: int = 2) -> list[dict[str, Any]]:
    """生存不足：开战即被打崩、阵亡比例高、输出时间被压缩。"""
    rng = random.Random(seed)
    ev: list[dict[str, Any]] = [_squad_event(T0), _scene_event(T0 + 50),
                               _battle_state(T0 + 100, True)]
    hp = {101: 42_000.0, 102: 30_000.0, 103: 28_000.0, 104: 26_000.0}
    for i in range(0, 60_000, 3_000):
        ts = T0 + 500 + i
        for inst in (101, 102, 103, 104):
            hp[inst] -= 3_500 + rng.random() * 800
            ev.append(_damage(ts, 9001, inst, 3_500 + rng.random() * 800,
                              max(0.0, hp[inst])))
        ev.append(_damage(ts + 300, 102, 9001, 2_000, 700_000 - i * 1_000))
    ev.append(_entity_die(T0 + 20_000, 104))
    ev.append(_entity_die(T0 + 30_000, 103))
    ev.append(_entity_die(T0 + 40_000, 102))
    ev.append(_entity_die(T0 + 55_000, 101))
    ev.append(_battle_state(T0 + 60_000, False))
    return ev


def scenario_skill_rotation_error(seed: int = 3) -> list[dict[str, Any]]:
    """技能循环错误：技能几乎不放、超长空窗、增益断档、输出集中在一人。"""
    rng = random.Random(seed)
    ev: list[dict[str, Any]] = [_squad_event(T0), _scene_event(T0 + 50),
                               _battle_state(T0 + 100, True)]
    duration = 120_000
    ev.append(_skill_event(T0 + 1_000, 101, "skill_open"))    # 只在开头放 2 次
    ev.append(_skill_event(T0 + 6_000, 101, "skill_open2"))   # 之后 100 秒空窗
    ev.append(_add_buff(T0 + 500, 1, 104, 102, "atk_up"))
    for i in range(0, duration, 4_000):
        ts = T0 + 1_000 + i
        ev.append(_damage(ts, 102, 9001, 3_000 + rng.random() * 500, 500_000 - i * 800))
        if i >= 40_000 and i % 12_000 == 0:
            ev.append(_damage(ts + 500, 9001, 101, 2_500, max(1.0, 40_000 - i * 5)))
    ev.append(_finish_buff(T0 + 30_000, 1))                   # 增益结束但从不续接
    ev.append(_finish_buff(T0 + 45_000, 1))
    ev.append(_finish_buff(T0 + 60_000, 1))
    ev.append(_battle_state(T0 + duration, False))
    return ev


def scenario_configuration_error(seed: int = 4) -> list[dict[str, Any]]:
    """配置错误：编队只有 2 人、含重复角色、无队长、速败零伤害。"""
    short_party = [
        {"battle_inst_id": 201, "templateid": "chr_dup", "display_name": "同角色A",
         "is_leader": False, "squad_index": 0},
        {"battle_inst_id": 202, "templateid": "chr_dup", "display_name": "同角色B",
         "is_leader": False, "squad_index": 2},
    ]
    ev: list[dict[str, Any]] = [_squad_event(T0, short_party),
                               _scene_event(T0 + 50, short_party),
                               _battle_state(T0 + 100, True)]
    ev.append(_damage(T0 + 1_000, 9001, 201, 50_000, 0.0))    # 速败：8 秒内全灭且零伤害
    ev.append(_entity_die(T0 + 1_200, 201))
    ev.append(_damage(T0 + 3_000, 9001, 202, 50_000, 0.0))
    ev.append(_entity_die(T0 + 3_200, 202))
    ev.append(_battle_state(T0 + 8_000, False))
    return ev


def scenario_victory(seed: int = 5) -> list[dict[str, Any]]:
    """正常通关（负样本）：检验规则引擎**不会**乱报失败原因。"""
    rng = random.Random(seed)
    ev: list[dict[str, Any]] = [_squad_event(T0), _scene_event(T0 + 50),
                               _battle_state(T0 + 100, True)]
    duration = 45_000
    ev.append(_add_buff(T0 + 300, 1, 104, 102, "atk_up"))
    for i in range(0, duration, 3_000):
        ts = T0 + 800 + i
        ev.append(_skill_event(ts, 101, f"sk{i}"))
        ev.append(_passive(ts + 100, 700 + i))
        for inst in (101, 102, 103, 104):
            ev.append(_damage(ts + 200, inst, 9001, 4_000 + rng.random() * 500,
                              max(0.0, 400_000 - i * 30_000)))
    ev.append(_entity_die(T0 + 40_000, 9001))
    ev.append(_battle_state(T0 + duration, False))
    return ev


#: 场景名 → (生成函数, 期望标签)。None = 不得误报失败。
SCENARIOS: dict[str, tuple[Any, str | None]] = {
    "dps_insufficient": (scenario_dps_insufficient, "dps_insufficient"),
    "survivability_insufficient": (scenario_survivability_insufficient,
                                   "survivability_insufficient"),
    "skill_rotation_error": (scenario_skill_rotation_error, "skill_rotation_error"),
    "configuration_error": (scenario_configuration_error, "configuration_error"),
    "victory": (scenario_victory, None),
}


def generate(scenario: str | None = None, count: int = 1, seed: int = 0) -> list[dict[str, Any]]:
    """生成事件流。`scenario=None` 时按 count 轮转全部场景。"""
    if scenario and scenario in SCENARIOS:
        fn, _ = SCENARIOS[scenario]
        out: list[dict[str, Any]] = []
        for i in range(max(1, count)):
            out.extend(fn(seed + i))
        return out
    out = []
    names = list(SCENARIOS)
    for i in range(max(1, count)):
        fn, _ = SCENARIOS[names[i % len(names)]]
        out.extend(fn(seed + i))
    return out


def _retag(events: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
    """换 session_id，避免多场战斗互相串味。"""
    out = []
    for event in events:
        clone = copy.deepcopy(event)
        clone["session_id"] = session_id
        out.append(clone)
    return out


def generate_sessions(scenario: str | None = None, count: int = 1, seed: int = 0,
                      session_prefix: str = "synthetic") -> list[list[dict[str, Any]]]:
    """按「一场战斗一组事件」返回，便于生成多条独立样本。"""
    groups: list[list[dict[str, Any]]] = []
    names = list(SCENARIOS)
    for i in range(max(1, count)):
        name = scenario if (scenario and scenario in SCENARIOS) else names[i % len(names)]
        fn, _ = SCENARIOS[name]
        groups.append(_retag(fn(seed + i), f"{session_prefix}-{name}-{i:05d}"))
    return groups


def demo_batch() -> list[dict[str, Any]]:
    """一次拿全部场景的事件（自检/演示用）。"""
    out: list[dict[str, Any]] = []
    for fn, _ in SCENARIOS.values():
        out.extend(fn())
    return out