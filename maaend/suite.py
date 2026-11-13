"""maa_end 套组集成层 —— NitroGen 接口 + 游戏决策层接口。

MaaEnd / MAA / BetterGI 同属一个架构范式：【截图 → 识别 → 决策 → 执行】的视觉
闭环（MaaEnd 官网原话；BetterGI 的 YOLOv8+OCR 识别 + 调度器同构）。本插件套组
按同一分层落位，本模块只做「决策」和「套组仲裁」两层的接线：

- 识别层：本插件自带 YOLO（yolo.py，BetterGI 同款 ONNX/YOLO 路线）；
- 决策层：game_decision 插件（gd_route / gd_decide / gd_status，laya System-1
  类型化决策，一次前向输出概率，无文本生成）；
- 执行层：本插件的 MaaPiCli 运行器（MAA 官方 PI 语义）与 NitroGen 游戏代理
  （nitrogen_agent 插件，帧→手柄动作）；
- 套组仲裁：nitrogen_agent 的 suite 模式（决策层为主，NitroGen 需显式接管）。
  本插件是对等消费方：启动任务前查谁在玩，并暴露约定式 status 入口供探测。

本模块不直接 import SDK（SuiteClient 注入 plugins 访问器；p2p_http 仅包内加载），
纯逻辑部分（route_text / match_tasks / merge_ranking）可直接单测。
"""

from __future__ import annotations

import re
from typing import Any

try:  # shortcut: #3483 运行期 plugins.call_entry*/require_enabled 静默超时，改走宿主 HTTP /runs；上游修复后删掉 p2p_http 回到 SDK
    from .p2p_http import call_entry_fallback, require_enabled_fallback
except ImportError:  # 纯逻辑单测按顶层模块加载（无包上下文/无 SDK）：保持原 SDK 路径
    call_entry_fallback = require_enabled_fallback = None

# ---------------------------------------------------------------------------
# 官方资源（来自 maaend.com / maa.plus / bettergi.com，2026-09-28 核对）
# ---------------------------------------------------------------------------

LINKS: dict[str, str] = {
    "MaaEnd 官网": "https://maaend.com/",
    "MaaEnd 源码": "https://github.com/MaaEnd/MaaEnd",
    "MaaEnd 文档": "https://github.com/MaaEnd/MaaEnd/blob/v2/README.md",
    "Mirror酱高速下载": "https://mirrorchyan.com/zh/projects?rid=MaaEnd&source=maaend.com-link",
    "终末地一图流": "https://ef.yituliu.cn/",
    "MAA 官网": "https://maa.plus/",
    "MAA 源码": "https://github.com/MaaAssistantArknights",
    "MAA 文档": "https://maa.plus/docs/",
    "MaaFramework": "https://github.com/MaaXYZ/MaaFramework",
    "MaaFramework 官网": "https://maafw.com/",
    "BetterGI 官网": "https://www.bettergi.com/",
    "BetterGI 源码": "https://github.com/babalae/better-genshin-impact",
    "BetterGI 文档": "https://bettergi.com/doc.html",
    "NitroGen 源码": "https://github.com/MineDojo/NitroGen",
    "NitroGen 权重": "https://huggingface.co/nvidia/NitroGen",
}


# ---------------------------------------------------------------------------
# 纯逻辑：路由文案（与 nitrogen_agent._route_text 同一约定，跨插件口径一致）
# ---------------------------------------------------------------------------

def route_text(route: dict[str, Any], *, short: bool = False) -> str:
    if not route:
        return "决策层无返回"
    if route.get("error"):
        return f"决策层不可用({route['error']})"
    game = route.get("game") or "未判定"
    src = route.get("game_source") or "?"
    intent = route.get("intent") or "?"
    head = f"游戏《{game}》({src}) / 意图 {intent}"
    if short:
        return head
    targets = route.get("targets") or []
    if not targets:
        return head + ";套组内暂无在线接手插件"
    parts = []
    for t in targets[:5]:
        if not isinstance(t, dict):
            continue
        pid = str(t.get("plugin") or t.get("id") or "?")
        ents = t.get("entries") or []
        parts.append(pid + (f"[{'/'.join(str(e) for e in ents[:2])}]" if ents else "(能力型)"))
    return head + ";可接手:" + ", ".join(parts)


# ---------------------------------------------------------------------------
# 纯逻辑：任务清单 ↔ 用户请求 的确定性匹配（决策层不可用时的如实回退，
# 不冒充决策——分数只代表词面重合，结果里会标注 source="keyword"）
# ---------------------------------------------------------------------------

_ASCII_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _terms(text: str) -> set[str]:
    """ASCII 拆词 + CJK 单字。中文任务名（如 日常/基质/打桩）按字面命中即可。"""
    low = str(text or "").lower()
    out = set(_ASCII_WORD.findall(low))
    out.update(ch for ch in low if _CJK.match(ch))
    return {t for t in out if len(t) > 1 or _CJK.match(t)}


def match_tasks(
    request: str, tasks: list[dict[str, Any]], limit: int = 5
) -> list[dict[str, Any]]:
    """词面匹配排序：[{task, score, why}]（score ∈ (0,1]，越高越相关）。"""
    q = _terms(request)
    ranked: list[dict[str, Any]] = []
    for task in tasks or []:
        hay = " ".join(
            str(task.get(k) or "")
            for k in ("name", "entry", "description")
        ) + " " + " ".join(
            str(opt) for opt in (task.get("option_names") or [])
        )
        hit = q & _terms(hay)
        if not hit:
            continue
        score = round(len(hit) / max(1.0, len(q) ** 0.5), 4)
        ranked.append({
            "task": {k: task.get(k) for k in ("name", "entry", "description")},
            "score": score,
            "why": "词面命中：" + "、".join(sorted(hit)[:6]),
        })
    ranked.sort(key=lambda r: -r["score"])
    return ranked[: max(1, limit)]


def merge_ranking(
    tasks: list[dict[str, Any]],
    probabilities: dict[str, float] | None,
    keyword_hits: list[dict[str, Any]] | None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """决策层概率为主、词面匹配兜底，合成最终候选排序。

    probabilities: {entry_or_name: p}（gd_decide 一次前向的 noul 概率）。
    未进决策层的任务若词面命中也保留（source 标注），排序永远按
    (决策概率可得, 分数) 降序——不混造数值。
    """
    probabilities = probabilities or {}
    by_key = {str(t.get("name")): t for t in tasks or []}
    by_key.update({str(t.get("entry")): t for t in tasks or [] if t.get("entry")})
    merged: dict[str, dict[str, Any]] = {}
    for key, p in probabilities.items():
        task = by_key.get(key)
        if task is None:
            continue
        merged[key] = {
            "task": {k: task.get(k) for k in ("name", "entry", "description")},
            "score": round(float(p), 4),
            "source": "decision",
            "why": f"决策层判定 p={float(p):.2f}",
        }
    for hit in keyword_hits or []:
        key = str(hit["task"].get("name"))
        cur = merged.get(key)
        if cur is None or hit["score"] > cur["score"] and cur["source"] != "decision":
            merged[key] = {**hit, "source": "keyword"}
    out = sorted(
        merged.values(),
        key=lambda r: (r["source"] == "decision", float(r["score"])),
        reverse=True,
    )
    return out[: max(1, limit)]


# ---------------------------------------------------------------------------
# IPC 客户端：注入 router.plugins 访问器，任何失败以 {"error": ...} 返回不抛
# ---------------------------------------------------------------------------

class SuiteClient:
    """game_decision / nitrogen_agent 的只读调用客户端。"""

    def __init__(self, plugins: Any, layer_id: str = "game_decision",
                 agent_id: str = "nitrogen_agent"):
        self.plugins = plugins
        self.layer_id = layer_id or "game_decision"
        self.agent_id = agent_id or "nitrogen_agent"

    async def call(self, plugin_id: str, entry: str,
                   args: dict[str, Any] | None = None,
                   timeout: float = 8.0) -> dict[str, Any]:
        ref = f"{plugin_id}:{entry}"
        try:
            call_json = getattr(self.plugins, "call_entry_json", None)
            if call_json is not None and call_entry_fallback is not None:  # shortcut: #3483
                res = await call_entry_fallback(self.plugins, ref, args or {}, timeout=timeout, json_only=True)
            elif call_json is not None:
                res = await call_json(ref, args or {}, timeout=timeout)
            else:  # ponytail: 旧视图无 call_entry_json/超时,靠入口自身 timeout 兜底
                res = await self.plugins.call_entry(ref, args or {})
        except Exception as exc:  # noqa: BLE001 - 跨插件失败一律降级为 error 字典
            return {"error": f"{type(exc).__name__}: {exc}", "plugin": plugin_id, "entry": entry}
        if hasattr(res, "is_err") and res.is_err():
            return {"error": str(getattr(res, "error", "调用失败")),
                    "plugin": plugin_id, "entry": entry}
        val = getattr(res, "value", None)
        return val if isinstance(val, dict) else {
            "error": f"返回非对象: {type(val).__name__}", "plugin": plugin_id, "entry": entry}

    # ---- 决策层（game_decision）----

    async def layer_enabled(self) -> bool | None:
        try:
            if require_enabled_fallback is not None:  # shortcut: #3483
                res = await require_enabled_fallback(self.plugins, self.layer_id, timeout=5.0)
            else:
                res = await self.plugins.require_enabled(self.layer_id, timeout=5.0)
            return not (hasattr(res, "is_err") and res.is_err())
        except Exception:  # noqa: BLE001
            return None

    async def route(self, query: str, context: str = "",
                    timeout: float = 25.0) -> dict[str, Any]:
        return await self.call(self.layer_id, "gd_route",
                               {"query": str(query or ""), "context": str(context or "")},
                               timeout=timeout)

    async def decide(self, text: str, questions: dict[str, Any],
                     timeout: float = 25.0) -> dict[str, Any]:
        return await self.call(self.layer_id, "gd_decide",
                               {"text": str(text or ""), "questions": questions},
                               timeout=timeout)

    async def layer_status(self, timeout: float = 25.0) -> dict[str, Any]:
        return await self.call(self.layer_id, "gd_status", {}, timeout=timeout)

    # ---- NitroGen（nitrogen_agent）----

    async def nitrogen_status(self, timeout: float = 8.0) -> dict[str, Any]:
        return await self.call(self.agent_id, "get_agent_status", {}, timeout=timeout)

    async def suite_status(self, probe: bool = False,
                           timeout: float = 25.0) -> dict[str, Any]:
        return await self.call(self.agent_id, "suite_status", {"probe": bool(probe)},
                               timeout=timeout)

    async def set_suite_mode(self, mode: str, confirm: bool = False,
                             timeout: float = 15.0) -> dict[str, Any]:
        """切换套组模式（nitrogen 需 confirm=true；design §4.3：确认由调用方持有）。"""
        return await self.call(self.agent_id, "set_suite_mode",
                               {"mode": str(mode), "confirm": bool(confirm)}, timeout=timeout)

    async def start_agent(self, process: str, force: bool = False,
                          timeout: float = 30.0) -> dict[str, Any]:
        """启动 NitroGen 游戏代理（M3；无任务概念，process 为目标进程名）。"""
        return await self.call(self.agent_id, "start_game_agent",
                               {"process": str(process), "force": bool(force)}, timeout=timeout)

    async def stop_agent(self, timeout: float = 20.0) -> dict[str, Any]:
        return await self.call(self.agent_id, "stop_game_agent", {}, timeout=timeout)

    def nitrogen_playing(self, status: dict[str, Any]) -> dict[str, Any]:
        """从 get_agent_status 里取「NitroGen 是否在玩 + 目标进程」。"""
        if status.get("error"):
            return {"known": False, "running": False, "error": status.get("error")}
        agent = status.get("agent") if isinstance(status.get("agent"), dict) else {}
        return {
            "known": True,
            "running": bool(agent.get("running")),
            "target_process": str(agent.get("target_process") or ""),
        }
