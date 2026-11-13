"""事件拆包 —— WS 帧 / 日志行 → 事件列表。

合并历史（docs/REFACTOR-PLAN.md 阶段 5 #1）：
这段逻辑原本有两份，逐行同构、只有局部变量名不同：

- `maaend/assist.py::_unwrap`（从 WS 帧与 logs 行还原）
- `maaend/assist_rules.py::unwrap_events`（规则引擎侧同一个协议）

两份都在解析同一个协议（ZMDLogs 的 hello / event_batch / 裸事件），
任何一方单独改动都会让「实时辅助」和「离线报告」对同一份日志得出不同结果。
现在只有这一份实现，另外两处保留原函数名作为转发。
"""

from __future__ import annotations

from typing import Any


def unwrap_events(payload: Any) -> list[dict[str, Any]]:
    """从 WS 帧或日志行还原事件列表。

    三种形态（ZMDLogs 协议）：

    - `{"type": "hello"}` —— 握手帧，不含事件，返回空；
    - `{"type": "event_batch", "events": [...], "session_id": "..."}` ——
      批量帧，`session_id` 会补到缺这个字段的单条事件上（同批次属于同一场战斗）；
    - 其余带 `type` 的对象 —— 单条裸事件，原样返回。

    任何非法输入（非 dict / events 不是 list / 无 type）都返回空列表，从不抛异常：
    这是消费端，上游是网络与日志，不能因为一帧脏数据把整轮 poll 打断。
    """
    if not isinstance(payload, dict):
        return []
    kind = payload.get("type")
    if kind == "hello":
        return []
    if kind == "event_batch":
        events = payload.get("events")
        if not isinstance(events, list):
            return []
        session_id = payload.get("session_id")
        out: list[dict[str, Any]] = []
        for item in events:
            if not isinstance(item, dict):
                continue
            if item.get("session_id") is None and session_id is not None:
                item = {**item, "session_id": session_id}
            out.append(item)
        return out
    if not kind:
        return []
    return [payload]
