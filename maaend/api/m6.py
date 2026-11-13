"""M6 GameScaling 视觉语言代理。

由 `maaend/router.py` 拆分而来（2026-11-04，见 docs/REFACTOR-PLAN.md 阶段 3）。
状态一律经 `self.state`（AppState）读写，不直接持有实例字段。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from plugin.sdk.plugin import (
    Err,
    Ok,
    SdkError,
    llm_tool,
    plugin_entry,
)

from ._helpers import game_window


class M6API:
    """M6 GameScaling 视觉语言代理。"""

    @plugin_entry(
        id="ef_gamescaling",
        name="GameScaling 视觉语言代理（M6）",
        description="M6：THUSI-Lab GameScaling（Qwen3.5 续训游戏 VLM）直接游玩——"
        "截图发用户自部署的 vLLM 服务，模型回 200ms 键鼠块，经 SendInput 真实注入。"
        "三重闸门：maa.gamescaling_enabled=true + maa.allow_run=true + confirm=true"
        "（缺一不可）。action=start 启动（前提：游戏开着、vLLM 在线、"
        "data/gamescaling/ 有系统提示词）；action=stop 停止并抬起全部按键；"
        "action=status（默认）看运行状态/解析率/延迟。与 M3 同级：无完成保证，人要看着。",
        timeout=60.0,
    )
    async def gamescaling(
        self,
        action: Annotated[str, "start / stop / status"] = "status",
        confirm: Annotated[bool, "start 必填 true（用户明确同意键鼠接管）"] = False,
        force: Annotated[bool, "套组互斥覆盖：NitroGen 正在玩时也强行启动（默认否）"] = False,
        **_,
    ):
        result = await self._gamescaling_impl(str(action or "status").lower(),
                                              confirm=bool(confirm), force=bool(force))
        if result.get("error"):
            return Err(SdkError(str(result["error"])))
        return Ok(result)

    async def _gamescaling_impl(
        self, action: str, *, confirm: bool, force: bool,
    ) -> dict[str, Any]:
        """ef_gamescaling / maa_gamescaling 共用实现；失败以 error 键返回。"""
        settings = await self._current_settings()
        if action not in ("start", "stop", "status"):
            return {"error": "action 只能是 start/stop/status"}

        if action == "stop":
            if self.state.m6 is None:
                return {"stopped": False, "note": "本会话还没启动过 GameScaling 代理。"}
            res = await self.state.m6.stop("ef_gamescaling action=stop")
            if self.state.mode == "m6":
                self.state.mode = ""
            return {"stopped": bool(res.get("stopped")), "mode": "m6",
                    "stop_reason": res.get("stop_reason") or res.get("reason"),
                    "note": "代理已停，按键已全部抬起。"}

        if action == "status":
            from ..backends.agent import policy_config_from_settings
            from ..backends.gamescaling import GameScalingPolicy

            cfg = policy_config_from_settings(settings)
            payload: dict[str, Any] = {
                "enabled": bool(settings.get("gamescaling_enabled")),
                "server_healthy": await asyncio.to_thread(
                    GameScalingPolicy("", cfg).health_check, 1.5),
                "url": cfg.base_url,
                "model": cfg.model,
                "reasoning": cfg.reasoning,
            }
            if self.state.m6 is not None:
                payload["agent"] = self.state.m6.status()
            else:
                payload["note"] = "本会话还没启动过（start 需 enabled + allow_run + confirm 三闸门）。"
            return payload

        # ---- action == "start"：三重闸门 ----
        if not settings.get("gamescaling_enabled"):
            return {"error": "maa.gamescaling_enabled 现在是 false（M6 总闸门默认关）。"
                             "要放行：改安装目录下 config/plugin.toml 的 [maa] "
                             "gamescaling_enabled = true，然后在插件面板重载本插件。"}
        gate = await self._gate_run()
        if gate is not None:
            return {"error": str(getattr(gate.error, "message", gate.error))}
        if not confirm:
            return {"error": "M6 会真实注入键盘鼠标（SendInput）接管游戏，且无完成保证。"
                             "经用户明确同意后带 confirm=true 重试。"}
        # 套组互斥：NitroGen 在玩时不双开（两套自动化会互相打架，同 run_task）
        if await self._suite_enabled() and bool(settings.get("suite_mutual_exclusion", True)) and not force:
            suite = self._suite_client(settings)
            n2 = suite.nitrogen_playing(await suite.nitrogen_status())
            if n2.get("running"):
                return {"error": f"NitroGen 游戏代理正在运行（目标进程 "
                                 f"{n2.get('target_process') or '?'}），同时开 M6 会互相打架。"
                                 "先停它（对话里说『停止 NitroGen 代理』），或确认后带 force=true 重试。"}
        game = game_window(settings)
        if not game.get("found"):
            return {"error": "游戏窗口未找到：先把终末地开到前台再启动 M6。"}
        refuse = self._verify_window(settings, game)
        if refuse:
            return {"error": refuse}

        from ..backends.agent import AgentError, GamescalingAgent

        if self.state.m6 is None:
            self.state.m6 = GamescalingAgent(log=self._m6_log)
        prompt_path = self.data_path("gamescaling") / str(
            settings.get("gamescaling_prompt") or "endfield_system_prompt.txt")
        try:
            started = await self.state.m6.start(settings=settings, window=game,
                                           prompt_path=prompt_path)
        except AgentError as exc:
            return {"error": str(exc)}
        self.state.mode = "m6"
        started["note"] = ("GameScaling 已接管（无完成保证，人要看着）："
                           "每 200ms 一个键鼠块；停止用 ef_gamescaling action=stop 或 ef_stop_task。")
        return started

    def _m6_log(self, msg: str) -> None:
        """代理日志：走宿主 report_status 通道（构造期不可用则丢弃）。"""
        try:
            self.report_status(msg)
        except Exception:  # noqa: BLE001 - 日志失败不影响代理
            pass

    @llm_tool(name="maa_gamescaling", description="GameScaling 视觉语言代理(M6):action=start 让 VLM 直接游玩(截图→200ms 键鼠块→SendInput 注入;需三闸门 maa.gamescaling_enabled=true + allow_run=true + confirm=true,缺一不可);action=stop 停止并抬起全部按键;action=status 查运行状态/解析率/延迟/服务健康。用户自备 vLLM 服务(gamescaling_url)与模型(gamescaling_model),插件只做客户端。与 M3 同级:接管输入、无完成保证。",
              parameters={"type": "object", "properties": {"action": {"type": "string", "enum": ["start", "stop", "status"]}, "confirm": {"type": "boolean", "description": "start 必填 true(用户明确同意键鼠接管)"}, "force": {"type": "boolean", "description": "NitroGen 正在玩时强行启动(默认否)"}}},
              timeout=60.0)
    async def maa_gamescaling_tool(self, **kwargs):
        action = str(kwargs.get("action") or "status")
        result = await self._gamescaling_impl(action, confirm=bool(kwargs.get("confirm")),
                                              force=bool(kwargs.get("force")))
        if result.get("error"):
            return {"output": result, "is_error": True, "error": str(result["error"])[:200]}
        return {"output": result}
