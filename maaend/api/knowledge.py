"""知识库查询、决策派单、套组接口。

由 `maaend/router.py` 拆分而来（2026-11-04，见 docs/REFACTOR-PLAN.md 阶段 3）。
状态一律经 `self.state`（AppState）读写，不直接持有实例字段。
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from plugin.sdk.plugin import (
    Err,
    Ok,
    SdkError,
    llm_tool,
    plugin_entry,
)

from .. import kb as kbstore
from ..backends.catalog import (
    tasks_of,
)
from ..backends.control import ControlClient
from ..constants import DOWNLOAD_URL  # noqa: E402
from ..suite import LINKS, match_tasks, merge_ranking, route_text


class KnowledgeAPI:
    """知识库查询、决策派单、套组接口。"""

    @plugin_entry(
        id="ef_kb",
        name="终末地知识库",
        description="查已采集的终末地资料：全文检索（干员/武器/物品/装备/敌人/剧情/任务/教学）、"
        "实体属性、干员清单、基建配方、MaaEnd 界面节点、辅助工具清单。数据来自 "
        "warfarin.wiki 全量、MaaEnd 界面流程、IndustrialPlanner 配方，均带来源与抓取时间。只读。",
        llm_result_fields=["action", "query", "result"],
        timeout=60.0,
    )
    async def kb_query(
        self,
        action: Annotated[
            str, "search=全文检索 | entity=实体详情 | operators=干员清单 | recipe=基建配方 | "
                 "node=界面节点 | tool=工具清单 | doc=单篇正文 | gamefile=解包产物索引 | "
                 "gametable=游戏配置表 | gem=基质反查武器 | poi=地图兴趣点 | stats=库存统计"] = "search",
        query: Annotated[str, "检索词 / 实体名 / 物品ID / 节点描述 / 文档 slug / 表名 / POI类型"] = "",
        limit: Annotated[int, "返回条数上限（默认 8）"] = 8,
        category: Annotated[str, "doc=分类限定；gamefile=组名；gametable=条目 key；poi=区域"] = "",
        near: Annotated[str, "poi 用：'x,y,z' 世界坐标，给了就按距离由近到远排"] = "",
        **_,
    ):
        if not kbstore.available():
            return Err(SdkError(
                "知识库未初始化：先运行 tools/kb/fetch_warfarin.py 与 tools/kb/build_kb.py"))
        act = (action or "search").strip().lower()
        n = max(1, min(50, int(limit or 8)))
        try:
            if act == "search":
                data = await asyncio.to_thread(kbstore.search, query, n)
            elif act == "entity":
                data = await asyncio.to_thread(kbstore.entity, query)
            elif act == "operators":
                data = await asyncio.to_thread(kbstore.operators, query)
            elif act == "recipe":
                data = await asyncio.to_thread(kbstore.recipe, query, n)
            elif act == "node":
                data = await asyncio.to_thread(kbstore.node, query, n)
            elif act == "tool":
                data = await asyncio.to_thread(kbstore.tools, keyword=query, status=category)
            elif act == "gem":
                # 基质反查武器：query=基质 ID（gem_xxx）或留空，category=标签组合
                # （attr_str+attr_atk+tactic）。数据来自本地解包表：
                # GemPresetTable → GemTagKeyToWeaponTable → WeaponBasicTable。
                data = await asyncio.to_thread(kbstore.gem_weapons, query, category, n)
            elif act == "doc":
                data = await asyncio.to_thread(kbstore.doc, category, query)
            elif act == "gamefile":
                data = await asyncio.to_thread(kbstore.gamefiles, query, category, n)
            elif act == "gametable":
                data = await asyncio.to_thread(kbstore.gametable, query, category, n)
            elif act == "poi":
                data = await asyncio.to_thread(kbstore.poi, query, category, near, n)
            elif act == "stats":
                data = await asyncio.to_thread(kbstore.stats)
            else:
                return Err(SdkError(
                    f"未知 action {act!r}；可选 search/entity/operators/recipe/node/tool/doc/"
                    f"gamefile/gametable/gem/poi/stats"))
        except FileNotFoundError as exc:
            return Err(SdkError(str(exc)))
        except Exception as exc:  # 只读查询不应拖垮宿主
            return Err(SdkError(f"知识库查询失败：{exc}"))
        return Ok({"action": act, "query": query, "result": data})

    @llm_tool(
        name="maa_endfield_lookup",
        description="查终末地资料库：干员/武器/物品/装备/敌人/剧情/任务/基建配方/界面操作路径/"
        "游戏配置表（解包得到的精确数值）/地图兴趣点（宝箱、篝火、BOSS 的精确坐标）。"
        "玩家问『XX 是谁』『XX 用什么合成』『XX 的突破材料』『最近的宝箱/篝火在哪』"
        "『游戏里有哪些数据表』时用。"
        "kind: search=全文检索 | entity=实体属性 | recipe=基建配方 | node=界面操作节点 | "
        "gametable=游戏配置表（query 填表名，category 填条目 key）| "
        "poi=地图兴趣点（query 填类型如「宝箱」，category 填区域，near 填 'x,y,z' 按距离排序）。"
        "返回条目均带来源，可直接引用给玩家。只读，不操作游戏。",
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "关键词、实体名、物品 ID、表名或 POI 类型"},
            "kind": {"type": "string",
                     "description": "search（默认）| entity | recipe | node | gametable | poi"},
            "key": {"type": "string", "description": "gametable 的条目 key；poi 的区域；"},
            "near": {"type": "string", "description": "poi 专用：'x,y,z' 世界坐标"}},
            "required": ["query"]},
    )
    async def maa_endfield_lookup(
        self,
        query: Annotated[str, "关键词 / 实体名 / 物品 ID / 表名 / POI 类型"] = "",
        kind: Annotated[str, "search | entity | recipe | node | gametable | poi | tool | gem"] = "search",
        key: Annotated[str, "gametable 条目 key / poi 区域"] = "",
        near: Annotated[str, "poi 的 'x,y,z' 坐标"] = "",
        **_,
    ):
        if not kbstore.available():
            return "知识库尚未初始化：需要先运行 tools/kb/ 下的采集与构建脚本。"
        k = (kind or "search").strip().lower()
        try:
            if k == "gem":
                # 基质反查武器：query=基质 ID，key=标签组合（如 attr_str+attr_atk+tactic）
                data = await asyncio.to_thread(kbstore.gem_weapons, query, key, 20)
            elif k == "tool":
                # 工具清单：用于回答「查数据/排轴/蓝图用什么站」这类问题。
                # 与 kb_query(action="tool") 同源（ef_tool 表），24 条。
                data = await asyncio.to_thread(kbstore.tools, keyword=query, status=key)
            elif k == "entity":
                data = await asyncio.to_thread(kbstore.entity, query)
            elif k == "recipe":
                data = await asyncio.to_thread(kbstore.recipe, query)
            elif k == "node":
                data = await asyncio.to_thread(kbstore.node, query)
            elif k == "gametable":
                data = await asyncio.to_thread(kbstore.gametable, query, key, 8)
            elif k == "poi":
                data = await asyncio.to_thread(kbstore.poi, query, key, near, 10)
            else:
                data = await asyncio.to_thread(kbstore.search, query)
        except Exception as exc:
            return f"查询失败：{exc}"
        return json.dumps(data, ensure_ascii=False)[:8000]

    @plugin_entry(
        id="ef_decide",
        name="决策层派单",
        description="把『帮我做终末地日常/基质/跑商…』这类请求交给游戏决策层"
        "（game_decision：gd_route 定意图 + gd_decide 对任务清单逐个打分），"
        "返回排序后的候选任务与理由；决策层不可用时退回词面匹配并如实标注 source。"
        "run=true 时直接执行第一候选（闸门与互斥同 ef_run_task）。",
        llm_result_fields=["summary", "candidates", "route", "engine"],
        timeout=120.0,
    )
    async def decide_task(
        self,
        request: Annotated[str, "用户请求原文，如『清一下终末地日常』"],
        limit: Annotated[int, "候选任务数上限（默认 5）"] = 5,
        run: Annotated[bool, "true=执行第一候选（需 maa.allow_run 放行）"] = False,
        **_,
    ):
        request = (request or "").strip()
        if not request:
            return Err(SdkError("request 不能为空：说说想让终末地做什么。"))
        settings = await self._current_settings()
        install = await self._install()
        if not install["interface_json"]:
            return Err(
                SdkError(f"未找到 MaaEnd 安装，拿不到任务清单。下载：{DOWNLOAD_URL}")
            )
        try:
            catalog = self._catalog(install)
        except ValueError as exc:
            return Err(SdkError(str(exc)))
        tasks = tasks_of(catalog)

        payload: dict[str, Any] = {"request": request, "task_count": len(tasks)}
        # 1) 决策层路由（哪款游戏/哪类意图）——失败不拦派单，只如实标注。
        route: dict[str, Any] = {}
        if await self._suite_enabled():
            route = await self._suite_client(settings).route(request)
        payload["route"] = route
        payload["route_text"] = route_text(route) if route else "决策层未启用"

        # 2) 逐任务打分：决策层 gd_decide 一次前向（noul），词面匹配兜底。
        keyword_hits = match_tasks(request, tasks, limit=limit)
        probabilities: dict[str, float] = {}
        engine = "keyword"
        if route and not route.get("error"):
            listing = "\n".join(
                f"[{i + 1}] {t.get('name')}（entry={t.get('entry')}）：{str(t.get('description') or '')[:80]}"
                for i, t in enumerate(tasks[:20])  # ponytail: 前 20 个任务进决策,全清单会超 state 上限
            )
            questions = {
                f"t{i + 1}": {
                    "type": "noul",
                    "instructions": f"编号 [{i + 1}] 的任务是否直接满足用户的请求？",
                }
                for i in range(min(20, len(tasks)))
            }
            verdict = await self._suite_client(settings).decide(
                f"用户请求：{request}\n\nMaaEnd 任务清单：\n{listing}", questions)
            if not verdict.get("error"):
                answers = verdict.get("answers") or {}
                for i, t in enumerate(tasks[:20]):
                    ans = answers.get(f"t{i + 1}")
                    # laya/kev/jev 的 noul 答案形如 {"noul": p}（game_decision._noul_of 同款）
                    p = ans.get("noul") if isinstance(ans, dict) else None
                    if isinstance(p, (int, float)):
                        probabilities[str(t.get("name"))] = float(p)
                engine = str(verdict.get("engine") or "decision")

        candidates = merge_ranking(tasks, probabilities, keyword_hits, limit=limit)
        if not candidates:
            return Err(
                SdkError(
                    f"任务清单（{len(tasks)} 个）里没有匹配「{request}」的候选："
                    f"用 ef_list_tasks 看全清单后直接点名任务；"
                    f"或确认后用 ef_agent 切 NitroGen 视觉代理（通用但无完成保证）。"
                )
            )
        payload["engine"] = engine
        payload["candidates"] = candidates
        best = candidates[0]["task"]["name"]
        payload["summary"] = (
            f"首选 {best}（{candidates[0]['source']}，{candidates[0]['why']}）；"
            f"共 {len(candidates)} 个候选；路由：{payload['route_text']}"
        )

        if run:
            started = await self.run_task(name=best)
            if isinstance(started, Err):
                return started
            payload["started"] = started.value
        return Ok(payload)

    @plugin_entry(
        id="status",
        name="套组探测状态",
        description="约定式状态入口（nitrogen_agent 等套组同伴探测『谁在玩』用）。"
        "running = MaaPiCli 是否正在执行任务。",
        timeout=15.0,
    )
    async def suite_probe_status(self, **_):
        entry = ""
        if self.state.m3_active:
            running = True
        elif self.state.mode == "m1" and isinstance(self.state.control, ControlClient):
            try:  # 探测必须廉价：3s 超时，失败按空闲
                st = await asyncio.to_thread(self.state.control.state, 3.0)
                running = bool(st.get("running"))
            except Exception:  # noqa: BLE001
                running = False
        else:
            running = bool(self.state.runner is not None and self.state.runner.running())
            if running:
                events = self.state.runner.last_run_events()
                entry = str(events.get("started_entry") or "")
        bits = f"（mode={self.state.mode}" + (f" entry={entry}" if entry else "") + ")" \
            if (self.state.mode or entry) else ""
        return Ok({
            "running": running,
            "playing": running,  # 同义键：套组 PLAYING_KEYS 约定
            "mode": self.state.mode or "",
            "started_entry": entry,
            "summary": ("执行中" + bits) if running else "空闲",
        })

    @plugin_entry(
        id="ef_suite",
        name="套组全景",
        description="游戏插件套组全景：决策层（game_decision）引擎链状态、NitroGen 代理"
        "（nitrogen_agent）谁在玩、套组模式仲裁、本插件执行层状态，以及官方资源链接"
        "（MaaEnd / MAA / MaaFramework / BetterGI / NitroGen）。",
        llm_result_fields=["summary", "mode", "decision_layer", "nitrogen", "links"],
        timeout=60.0,
    )
    async def suite_overview(self, **_):
        settings = await self._current_settings()
        suite = self._suite_client(settings)
        running = bool(self.state.runner is not None and self.state.runner.running())
        payload: dict[str, Any] = {
            "self": {"plugin": "maa_end", "running": running,
                     "runner_alive": running},
            "links": LINKS,
        }
        if not await self._suite_enabled():
            payload["summary"] = "套组集成已关闭（maa.suite_enabled=false）。"
            return Ok(payload)

        layer = await suite.layer_status()
        n2 = suite.nitrogen_playing(await suite.nitrogen_status())
        arb = await suite.suite_status(probe=True)
        mode = str(arb.get("mode") or "?") if not arb.get("error") else "未知"
        payload["decision_layer"] = (
            {"id": suite.layer_id, "error": layer.get("error")} if layer.get("error")
            else {"id": suite.layer_id, "summary": layer.get("summary"),
                  "laya_ready": layer.get("laya_ready")}
        )
        payload["nitrogen"] = {"id": suite.agent_id, **n2}
        payload["arbitration"] = arb
        payload["mode"] = mode
        bits = [f"模式 {mode}"]
        bits.append("本插件执行中" if running else "本插件空闲")
        bits.append(f"NitroGen {'在玩 ' + (n2.get('target_process') or '?') if n2.get('running') else '空闲'}")
        bits.append("决策层可用" if not layer.get("error") else "决策层不可用")
        payload["summary"] = "；".join(bits) + "。"
        return Ok(payload)
