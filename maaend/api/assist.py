"""陪玩辅助：面板状态、卡关佐证、参数写回、建议生成。

由 `maaend/router.py` 拆分而来（2026-11-04，见 docs/REFACTOR-PLAN.md 阶段 3）。
状态一律经 `self.state`（AppState）读写，不直接持有实例字段。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated, Any

from plugin.sdk.plugin import (
    Err,
    Ok,
    SdkError,
    llm_tool,
    plugin_entry,
    ui,
)

from ..assist import PROFILES, AssistConfig, profile_key
from ..constants import CONFIG_PREFIX  # noqa: E402
from ..infra.probe import (
    foreground_hwnd,
)
from ._helpers import game_window


class AssistAPI:
    """陪玩辅助：面板状态、卡关佐证、参数写回、建议生成。"""

    # 面板 api.call 走 hosted-surface 白名单，没有 @ui.action 会被拒为 action_not_exposed
    @ui.action(id="ef_assist", label="陪玩辅助")
    @plugin_entry(
        id="ef_assist",
        name="陪玩辅助",
        description="陪聊默认 + 卡关辅助。action=state（默认，看状态与最近建议）/ "
        "config（读三项参数）/ set（改参数，即时生效，无需重启）/ check（立即跑一次判定）/ "
        "advice（对给定事件做一次分层输出，供对话内联调用；events_json 留空时用实时缓冲，"
        "需辅助在跑，只覆盖进行中的战斗，缓冲可能为空）。"
        "visual=true 时在 state/check 结果里附「场景佐证」（截屏+场景分类，只读；"
        "佐证仅供参考，不参与判定）。"
        "只读订阅本地事件流，**不点击、不注入、不接管游戏**。",
        timeout=20.0,
    )
    async def assist(
        self,
        action: Annotated[str, "state / config / set / check / advice"] = "state",
        assist_mode: Annotated[str, "宏观建议 / 精细建议（空=不改）"] = "",
        confidence_threshold: Annotated[str, "0.70 / 0.80 / 0.85 / 0.90（空=不改）"] = "",
        consecutive_fail_threshold: Annotated[str, "2 / 3 / 5（空=不改）"] = "",
        enable: Annotated[str, "true/false（空=不改）"] = "",
        profile: Annotated[str, "A/B 配置档（空=用当前档）；改参数时写到该档"] = "",
        events_json: Annotated[str, "advice 用：事件 JSON 数组；留空=用实时缓冲（需辅助在跑，只覆盖进行中的战斗，缓冲可能为空）"] = "",
        visual: Annotated[str, "true=附场景佐证（截屏+场景分类，只读）"] = "",
        **_,
    ):
        act = str(action or "state").strip().lower()
        want_visual = str(visual or "").strip().lower() in ("1", "true", "yes", "on")

        if act == "config":
            settings = await self._current_settings()
            cfg = AssistConfig.from_settings(settings)
            return Ok({
                "config": {
                    "assist_mode": cfg.assist_mode,
                    "confidence_threshold": cfg.confidence_threshold,
                    "consecutive_fail_threshold": cfg.consecutive_fail_threshold,
                    "enabled": cfg.enabled,
                    # 双配置（A/B 档）：面板据此渲染两套参数与当前档
                    "assist_profile": cfg.profile,
                    "profiles": cfg.profiles,
                },
                "defaults": {
                    "assist_mode": "宏观建议",
                    "confidence_threshold": 0.85,
                    "consecutive_fail_threshold": 3,
                    "assist_profile": "A",
                },
                "options": {
                    "assist_mode": ["宏观建议", "精细建议"],
                    "confidence_threshold": [0.70, 0.80, 0.85, 0.90],
                    "consecutive_fail_threshold": [2, 3, 5],
                    "assist_profile": ["A", "B"],
                },
                "note": "改参数请用 action=set（写回配置并即时生效），"
                        "或在设置面板的 Select 里选。",
            })

        if act == "set":
            # 双配置：先定档（未给则用当前档），三个参数都写进该档的扁平键；
            # 同时保留旧的扁平键，避免其它只读旧键的消费方失效。
            cur = AssistConfig.from_settings(await self._current_settings())
            want_profile = str(profile or "").strip().upper() or cur.profile
            if want_profile not in PROFILES:
                return Err(SdkError(f"profile 只能是 A 或 B：{profile}"))
            patch: dict[str, Any] = {}
            if want_profile != cur.profile:
                patch["assist_profile"] = want_profile
            if assist_mode:
                v = str(assist_mode).strip()
                patch["assist_mode"] = v
                patch[profile_key(want_profile, "assist_mode")] = v
            if confidence_threshold:
                try:
                    fv = float(confidence_threshold)
                except ValueError:
                    return Err(SdkError(f"confidence_threshold 不是数字：{confidence_threshold}"))
                patch["confidence_threshold"] = fv
                patch[profile_key(want_profile, "confidence_threshold")] = fv
            if consecutive_fail_threshold:
                try:
                    iv = int(consecutive_fail_threshold)
                except ValueError:
                    return Err(SdkError(
                        f"consecutive_fail_threshold 不是整数：{consecutive_fail_threshold}"))
                patch["consecutive_fail_threshold"] = iv
                patch[profile_key(want_profile, "consecutive_fail_threshold")] = iv
            if enable:
                patch["assist_enabled"] = str(enable).strip().lower() in ("1", "true", "yes", "on")
            if not patch:
                return Err(SdkError("没有要改的参数：至少给 assist_mode / "
                                    "confidence_threshold / consecutive_fail_threshold / "
                                    "enable / profile 之一。"))
            return await self._assist_set(patch)

        if act == "check":
            if self.state.assist is None:
                return Ok({"ran": False, "reason": "卡关辅助未启动（可能 assist_enabled=false）"})
            try:
                events = await asyncio.to_thread(self.state.assist.reader.poll)
            except Exception as exc:  # noqa: BLE001
                return Ok({"ran": False, "reason": f"读取事件失败：{exc}"})
            # flush=True：手动检查不等战斗结束标记，立即切分当前缓冲
            produced = self.state.assist.process(events, flush=True)
            payload = {
                "ran": True,
                "events": len(events),
                "source": self.state.assist.reader.source,
                "advices": [a.as_dict() for a in produced],
                "counters": dict(self.state.assist.counters),
            }
            if want_visual:
                payload["scene_evidence"] = await self._assist_scene_evidence()
            return Ok(payload)

        if act == "advice":
            return await self._assist_advice(events_json)

        # 默认 state
        if self.state.assist is None:
            return Ok({
                "running": False,
                "reason": "未启动（assist_enabled=false 或启动失败）",
                "note": "陪聊本身不受影响：对话工具照常可用。",
            })
        payload = dict(self.state.assist.state())
        if want_visual:
            payload["scene_evidence"] = await self._assist_scene_evidence()
        return Ok(payload)

    async def assist_context(self) -> dict[str, Any]:
        """Hosted UI 面板的 props.state（只读；改参数走 ef_assist action=set）。"""
        settings = await self._current_settings()
        cfg = AssistConfig.from_settings(settings)
        base: dict[str, Any] = {
            "enabled": cfg.enabled,
            "config": {
                "assist_mode": cfg.assist_mode,
                "confidence_threshold": cfg.confidence_threshold,
                "consecutive_fail_threshold": cfg.consecutive_fail_threshold,
                # 双配置（A/B 档）：面板据此渲染两套参数与当前档
                "assist_profile": cfg.profile,
                "profiles": cfg.profiles,
            },
        }
        if self.state.assist is None:
            base.update({
                "running": False,
                "reason": "未启动（assist_enabled=false 或启动失败）",
                "counters": {},
                "recent": [],
            })
            return base
        base.update(self.state.assist.state())
        return base

    async def _assist_scene_evidence(self) -> dict[str, Any] | None:
        """给辅助结果附一张「现在是什么场景」的佐证（只读截屏+分类）。

        佐证仅供参考、不参与判定（判定侧接入另行评估）；任何一步失败都只
        返回 None，绝不阻塞主结果。复用 maa_scene 的截屏+分类路径。
        """
        try:
            res = await self._scene_now(3)
        except Exception as exc:  # noqa: BLE001 - 佐证失败不阻塞主结果
            self._log("warning", f"assist 场景佐证采集失败（忽略，不影响主结果）：{type(exc).__name__}: {str(exc)[:120]}")
            return None
        if not isinstance(res, dict) or res.get("error"):
            return None
        return {
            "class": res.get("class"),
            "confidence": res.get("confidence"),
            "is_ui": res.get("is_ui"),
            "infer_ms": res.get("infer_ms"),
            "note": "佐证用：不参与判定。",
        }

    def _assist_scene_probe_sync(self) -> dict[str, Any] | None:
        """卡关判定用的场景佐证采集（**仅游戏前台时**才采——不抢焦点）。

        与 `ef_assist visual=true` 的按需佐证不同：本方法运行在辅助的后台
        语境，同步执行一次采集+分类（每轮 process 最多一次），并复刻 M4 条件
        提醒的「非前台跳过」原则——`capture_window` 内含 `SetForegroundWindow`，
        只有游戏已在前台时它才是无感操作。任何失败/非前台都返回 None。
        """
        settings = self.state.settings
        if not settings or not bool(settings.get("scene_enabled", True)):
            return None
        try:
            win = game_window(settings)
            if not win.get("found"):
                return None
            hwnd = int(win["hwnd"])
            if foreground_hwnd() != hwnd:
                return None  # 游戏不在前台：不采集（截图会抢焦点）
            clf = self._scene_classifier(settings)
            from PIL import Image

            from ..vision.capture import capture_window
            from ..vision.frames import to_rgb

            width, height, bgr = capture_window(hwnd)
            res = clf.classify(Image.fromarray(to_rgb(width, height, bgr)), 3)
            return {
                "class": res.get("class"),
                "confidence": res.get("confidence"),
                "is_ui": res.get("is_ui"),
                "infer_ms": res.get("infer_ms"),
                "captured_at": int(time.time() * 1000),
                "source": "foreground_probe",
            }
        except Exception as exc:  # noqa: BLE001 - 佐证失败不阻塞判定
            self._log("warning", f"assist 场景佐证（前台探针）失败（忽略，不影响判定）：{type(exc).__name__}: {str(exc)[:120]}")
            return None

    async def _assist_set(self, patch: dict[str, Any]) -> Any:
        """写回三项参数并即时重建辅助服务（动态切换点）。

        `config.update` 收的是**段名做键**的嵌套字典（与 plugin.toml 的段对应），
        所以这里包一层 `{CONFIG_PREFIX: patch}`。
        """
        try:
            await self.config.update({CONFIG_PREFIX: patch}, timeout=5.0)
        except Exception as exc:  # noqa: BLE001 - 写配置失败要如实说
            return Err(SdkError(f"写入配置失败：{exc}"))
        self.state.settings = None
        self._restart_assist()
        # 注意：SDK 不会向同一进程回派 config_change，所以要主动刷新派生状态
        cfg = AssistConfig.from_settings({**(await self._current_settings()), **patch})
        return Ok({
            "updated": patch,
            "effective": {
                "assist_mode": cfg.assist_mode,
                "confidence_threshold": cfg.confidence_threshold,
                "consecutive_fail_threshold": cfg.consecutive_fail_threshold,
            },
            "note": "已即时生效（辅助服务已按新参数重建）。",
        })

    async def _assist_advice(self, events_json: str) -> Any:
        """对给定事件做一次分层输出（对话内联调用：贴一段日志就能得建议）。

        events_json 留空时回落实时缓冲的只读快照（需辅助在跑）。
        """
        text = str(events_json or "").strip()
        source = "paste"
        if not text:
            assist = self.state.assist
            if assist is None:
                return Err(SdkError("events_json 为空且卡关辅助未启动："
                                    "贴一段事件 JSON，或把 assist_enabled 设为 true。"))
            if not assist.cfg.enabled:
                return Err(SdkError("events_json 为空且卡关辅助已禁用（assist_enabled=false）："
                                    "贴一段事件 JSON，或开启辅助。"))
            payload = assist.pending_snapshot()
            if not payload:
                return Err(SdkError("events_json 为空且实时缓冲没有事件：缓冲只覆盖进行中的战斗，"
                                    "刚打完或刚手动 check 过会为空。贴一段事件 JSON 试试。"))
            source = "live"
        else:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                return Err(SdkError(f"events_json 不是合法 JSON：{exc}"))
            if isinstance(payload, dict):
                payload = [payload]
            if not isinstance(payload, list):
                return Err(SdkError("events_json 需要是数组或单个事件对象。"))
        try:
            # battle_from_events 而非 split_battles：真实输入可能是 ZMDLogs 的
            # hello / event_batch 信封（unwrap 后才是平铺事件）；平铺输入原样透传。
            # 临时 AssistService 在此无用（advice 全程走模块级纯函数），不构造。
            from ..assist import MACRO_BY_LABEL, battle_from_events, classify_events
            battles = battle_from_events(payload)
            out = []
            for battle in battles:
                result = classify_events(battle)
                out.append({
                    "session_id": battle.session_id,
                    "label": result.label,
                    "label_zh": result.label_zh,
                    "confidence": result.confidence,
                    "macro": MACRO_BY_LABEL.get(result.label, ""),
                    "metrics": result.metrics,
                })
            return Ok({"battles": len(out), "results": out, "source": source})
        except Exception as exc:  # noqa: BLE001
            return Err(SdkError(f"解析事件失败：{type(exc).__name__}: {exc}"))

    @llm_tool(
        name="maa_assist_advice",
        description="对终末地一段战斗日志做失败原因归因并给分层建议。"
        "用户贴日志/问『为什么打不过』时用。events_json 是事件 JSON 数组"
        "（也收单个事件对象或 hello/event_batch 信封）；留空则用实时缓冲"
        "（需卡关辅助在跑，只覆盖进行中的战斗，缓冲可能为空）。"
        "返回四类失败原因之一（DPS不足/生存不足/技能循环错误/配置错误）与建议，"
        "source 标明 paste/live。只读，不操作游戏。",
        parameters={"type": "object", "properties": {
            "events_json": {"type": "string", "description": "战斗事件 JSON 数组（或单个事件对象、hello/event_batch 信封）；留空=用实时缓冲（需辅助在跑，只覆盖进行中的战斗，缓冲可能为空）"}},
            "required": ["events_json"]},
    )
    async def maa_assist_advice(
        self,
        events_json: Annotated[str, "战斗事件 JSON 数组；留空=用实时缓冲（需辅助在跑，只覆盖进行中的战斗，缓冲可能为空）"] = "",
        **_,
    ):
        return await self._assist_advice(events_json)
