"""任务执行、模式阶梯、排轴、操作回放、M3 视觉代理。

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
    plugin_entry,
)

from ..backends.catalog import (
    controller_type_of,
    meta_of,
    options_of,
    tasks_of,
)
from ..backends.control import ControlError
from ..backends.pi_runner import OUTPUT_LOG_REL, PiRunner, RunnerError
from ..constants import DOWNLOAD_URL  # noqa: E402
from ..executor import (
    enter_nitrogen,
    exit_nitrogen,
    probe_modes,
)
from ._helpers import game_window


class TaskAPI:
    """任务执行、模式阶梯、排轴、操作回放、M3 视觉代理。"""

    @plugin_entry(
        id="ef_run_task",
        name="执行任务",
        description="经 MaaEnd 官方运行器 MaaPiCli（-d 直通模式，完整 PI 语义：选项 case、"
        "agent 子进程）执行一个任务：写好 PI 配置后启动运行器，立即返回。"
        "name 用 list_tasks 里的任务名；options_json 是选项选择映射，"
        "例如 {\"选项名\": \"case 名\"}（可用值见 list_tasks 的 option_definitions）。"
        "NitroGen 代理正在玩时会拒绝（force=true 覆盖）。",
        timeout=120.0,
    )
    async def run_task(
        self,
        name: Annotated[str, "任务名（先用 list_tasks 查，清单随 MaaEnd 版本变化）"],
        options_json: Annotated[str, "选项选择映射，JSON 对象，例如 {\"SellProductSchedule\": \"Daily\"}；默认空"] = "",
        force: Annotated[bool, "套组互斥覆盖：NitroGen 正在玩时也强行启动（默认否）"] = False,
        controller: Annotated[str, "显式控制器名（空=自动：Windows 优先 Win32）；ADB/CloudADB 走手机端"] = "",
        adb_serial: Annotated[str, "ADB 控制器的设备地址（adb devices 的 serial，如 127.0.0.1:5555）"] = "",
        **_,
    ):
        gate = await self._gate_run()
        if gate is not None:
            return gate

        # 套组互斥：NitroGen 代理在玩时不双开（两套自动化会互相打架）。
        settings = await self._current_settings()
        if await self._suite_enabled() and bool(settings.get("suite_mutual_exclusion", True)) and not force:
            suite = self._suite_client(settings)
            n2 = suite.nitrogen_playing(await suite.nitrogen_status())
            if n2.get("running"):
                target = n2.get("target_process") or "?"
                return Err(
                    SdkError(
                        f"NitroGen 游戏代理正在运行（目标进程 {target}），同时开 MaaPiCli 会互相打架。"
                        f"先停止它（对话里说『停止 NitroGen 代理』或 nitrogen_agent 面板），"
                        f"或确认要并行后带 force=true 重试。"
                    )
                )

        name = (name or "").strip()
        if not name:
            return Err(SdkError("name 不能为空：先用 list_tasks 查任务清单。"))

        install = await self._install()
        if not install["found"]:
            return Err(
                SdkError(
                    f"未找到 MaaEnd 安装，无法执行任务。下载：{DOWNLOAD_URL}；"
                    f"装好后重载本插件。"
                )
            )

        try:
            catalog = self._catalog(install)
        except ValueError as exc:
            return Err(SdkError(str(exc)))
        tasks = tasks_of(catalog)
        task = next((t for t in tasks if name in (t["name"], t["entry"])), None)
        if task is None:
            return Err(SdkError(f"任务清单里没有 {name!r}：先用 list_tasks 查。"))

        settings = await self._current_settings()

        # 控制器：显式指定优先（校验在清单里）；否则 Windows 优先 Win32。
        meta = meta_of(catalog)
        controller = (controller or "").strip() or next(
            (c for c in meta["controllers"] if str(c).startswith("Win32")),
            meta["controllers"][0] if meta["controllers"] else "Win32-Front",
        )
        if meta["controllers"] and controller not in meta["controllers"]:
            return Err(
                SdkError(f"控制器 {controller!r} 不在清单里：{'、'.join(meta['controllers'])}")
            )
        is_adb = controller_type_of(catalog, controller) == "Adb"
        adb_binding: dict[str, str] | None = None
        if is_adb:
            serial = (adb_serial or "").strip()
            if not serial:
                return Err(
                    SdkError("ADB 控制器需要 adb_serial（设备地址，如 127.0.0.1:5555）：先用 ef_adb 查设备列表。")
                )
            adb_path = str(settings.get("adb_path") or "adb")
            # name 仅展示用，对齐 MaaPiCli 手动输入格式 "{address}-{adb_path}"
            adb_binding = {"name": f"{serial}-{adb_path}", "adb_path": adb_path, "address": serial}

        # Win32 控制器：窗口预检（ADB 控制器无窗口概念，跳过）。
        game: dict[str, Any] = {"found": False, "class": "", "title": ""}
        if not is_adb:
            game = game_window(settings)
        if not game["found"]:
            return Err(
                SdkError(
                    "游戏窗口未找到（按窗口类名/标题正则匹配）。先启动《明日方舟：终末地》"
                    "PC 客户端并进入主界面，再重试；或调整 maa.window_class_regex / "
                    "maa.window_title_regex。"
                )
            )
        if not is_adb:
            refused = self._verify_window(settings, game)
            if refused:
                return Err(SdkError(refused))

        # 选项映射：{选项名: case 名 / 布尔 / 输入对象}
        options: list[dict[str, Any]] = []
        parsed: dict[str, Any] = {}
        text = (options_json or "").strip()
        if text:
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                return Err(SdkError(f"options_json 不是合法 JSON：{exc}"))
            if not isinstance(parsed, dict):
                return Err(SdkError("options_json 必须是 JSON 对象，例如 {\"选项名\": \"case 名\"}"))
            definitions = options_of(catalog)
            for opt_name, value in parsed.items():
                entry_opt: dict[str, Any] = {"name": str(opt_name)}
                if isinstance(value, bool):
                    entry_opt["value"] = "true" if value else "false"
                elif isinstance(value, list):
                    entry_opt["values"] = [str(v) for v in value]
                elif isinstance(value, dict):
                    entry_opt["inputs"] = {str(k): str(v) for k, v in value.items()}
                else:
                    entry_opt["value"] = str(value)
                definition = definitions.get(str(opt_name))
                if isinstance(definition, dict) and definition.get("type"):
                    entry_opt["type"] = definition["type"]
                options.append(entry_opt)

        # ---- 模式调度（DESIGN §4.1）：M1 在线走控制服务，缺席回退下方 M2 链路 ----
        control = await self._control_client()
        if control is not None and await asyncio.to_thread(control.ensure_started):
            try:
                result = await asyncio.to_thread(
                    control.run, task["name"], parsed,
                    (controller if (controller or "").strip() else ""), (adb_serial or ""))
            except ControlError as exc:
                # 任务/窗口已在本地预检过，服务端拒绝属异常路径：如实返回，不静默重跑
                return Err(SdkError(f"M1 控制服务拒绝：{exc}"))
            self.state.mode = "m1"
            self.state.runner = None  # M1 会话不经本地 PiRunner
            result["mode"] = "m1"
            return Ok(result)
        if control is not None:
            self._log("warning", "MaaEndControl 不可用，回退内置 PiRunner（M2）")

        # 运行器目录与 PI 配置。
        runner_dir = self.data_path("maa-pi-runner")
        runner = PiRunner(runner_dir, install["root"])
        try:
            report = runner.prepare()
            self._log("info", f"MaaPiCli 运行目录就绪：{report}")
        except RunnerError as exc:
            return Err(SdkError(str(exc)))

        meta = meta_of(catalog)
        resource = meta["resources"][0] if meta["resources"] else ""
        if not resource:
            return Err(SdkError("interface.json 没有声明 resource，无法加载流水线。"))

        # PI 配置：ADB 控制器带 adb 块（三键必填，v5.14.0 AdbConfig）；
        # 窗口字段仅 Win32 有意义（config 侧实际靠 interface.json 的 regex 绑定）。
        config_path = runner.write_config(
            controller=controller,
            resource=resource,
            window_class=str(game.get("class") or ""),
            window_title=str(game.get("title") or ""),
            tasks=[{"name": task["name"], "option": options}],
            adb=adb_binding,
        )

        timeout_seconds = float(settings.get("run_timeout_seconds") or 0)
        try:
            runner.spawn(timeout_seconds=timeout_seconds, need_elevate=not is_adb)
        except RunnerError as exc:
            return Err(SdkError(str(exc)))
        except OSError as exc:
            return Err(SdkError(f"MaaPiCli 启动失败：{exc}"))

        self.state.runner = runner
        self.state.mode = "m2"
        self._log(
            "info",
            f"MaaPiCli 已启动：task={task['name']} controller={controller} "
            f"resource={resource} config={config_path.name}",
        )
        return Ok(
            {
                "started": True,
                "task": task["name"],
                "entry": task["entry"],
                "controller": controller,
                "window": game["title"] or None,
                "adb_serial": (adb_binding or {}).get("address") or None,
                "runner_dir": str(runner.dir),
                "options": options,
                "note": "MaaPiCli 在后台执行全部任务后自动退出：用 task_state 轮询，"
                f"stop_task 可停止。输出日志：{OUTPUT_LOG_REL.as_posix()}",
            }
        )

    @plugin_entry(
        id="ef_endaxis",
        name="排轴预设",
        description="3 套 Endaxis 排轴预设（开荒/精英/BOSS）。action=list 列预设；"
        "code 生成数据码（可粘进 MaaEnd 排轴设置）；compare 解析社区分享的轴码并与预设对比结构；"
        "run 经 RealTimeTask 的自动战斗排轴模式执行（与 ef_run_task 同一闸门与互斥）。"
        "members 是队伍人数 1-4，1 号位放主 C。",
        timeout=120.0,
    )
    async def endaxis(
        self,
        action: Annotated[str, "list | code | compare | run"] = "list",
        code: Annotated[str, "compare 用：社区分享的轴数据码（base64url+gzip）"] = "",
        preset: Annotated[str, "开荒 | 精英 | BOSS"] = "开荒",
        members: Annotated[int, "队伍人数 1-4（超出号位的动作会被丢掉）"] = 4,
        force: Annotated[bool, "run 时套组互斥覆盖（同 ef_run_task）"] = False,
        **_,
    ):
        from .. import endaxis as ea

        action = (action or "list").strip().lower()
        if action == "list":
            return Ok({"presets": [{"name": k, "description": v[0], "skip_combo_cooldown": v[1],
                                    "scenarios": [s[0] for s in v[2]]} for k, v in ea.PRESETS.items()]})
        if action == "compare":
            try:
                return Ok(ea.compare(code, (preset or "").strip(), members))
            except ValueError as exc:
                return Err(SdkError(str(exc)))
        if action not in ("code", "run"):
            return Err(SdkError(f"action 只能是 list / code / compare / run，收到 {action!r}"))
        try:
            project = ea.build_project((preset or "").strip(), members)
        except (ValueError, TypeError) as exc:
            return Err(SdkError(str(exc)))
        code = ea.encode(project)
        skip = ea.PRESETS[preset.strip()][1]
        if action == "code":
            return Ok({"preset": preset, "members": members, "code": code,
                       "skip_combo_cooldown": skip,
                       "scenarios": [s["name"] for s in project["scenarioList"]]})
        return await self.run_task(
            name="RealTimeTask",
            options_json=json.dumps(ea.run_options(code, skip), ensure_ascii=False),
            force=force)

    @plugin_entry(
        id="ef_task_state",
        name="任务进度",
        description="查询 MaaPiCli 运行器的状态：运行中 / 已退出（退出码）+ 输出日志尾部。",
        llm_result_fields=["running", "exit_code", "output_tail"],
        timeout=30.0,
    )
    async def task_state(self, **_):
        if self.state.mode != "m1" and self.state.runner is None:
            return Err(SdkError("还没有启动过任务：先 run_task。"))
        # ---- M1 分支：状态来自控制服务 ----
        if self.state.mode == "m1":
            control = await self._control_client()
            if control is None:
                return Err(SdkError("M1 会话但 control_url 已不可用：用 ef_modes 复查。"))
            try:
                payload = await asyncio.to_thread(control.state)
            except ControlError as exc:
                return Err(SdkError(f"控制服务状态读取失败：{exc}"))
            self.state.fails.observe(
                running=bool(payload.get("running")),
                failed=bool(payload.get("failed")),
                succeeded=bool(payload.get("succeeded")),
            )
            hint = self.state.fails.take_suggestion()
            if hint:
                payload["demotion_hint"] = hint
            payload["mode"] = "m1"
            return Ok(payload)
        # ---- M2 原链路 ----
        running = self.state.runner.running()
        code = self.state.runner.exit_code()
        events = self.state.runner.last_run_events()
        payload: dict[str, Any] = {
            "running": running,
            "exit_code": code,
            "started_entry": events.get("started_entry", ""),
            "output_tail": self.state.runner.output_tail(),
        }
        # 成功判定不靠 exit==0(SIGABRT 也 0,MaaPiCli 的 sig_handler 一律 exit 0),
        # 必须有 task start 事件 + 无失败事件才报完成。
        if running:
            entry = events.get("started_entry") or ""
            payload["summary"] = "MaaPiCli 正在执行任务" + (f"（entry={entry}）" if entry else "") + "。"
        elif events.get("failed"):
            payload["summary"] = "任务失败（maafw.log 有 Failed 事件）：看 output_tail 定位。"
        elif events.get("succeeded") and events.get("started_entry"):
            # Tasker.Task.Succeeded 事件 = 权威成功信号(exit 码反而不可靠)
            entry = events.get("started_entry") or ""
            payload["summary"] = "任务完成（Tasker.Task.Succeeded, entry=" + entry + "）。"
        elif events.get("started_entry") and code == 0:
            payload["summary"] = "MaaPiCli 已正常退出（任务完成）。"
        elif not events.get("started_entry") and code == 0:
            payload["summary"] = "退出码 0 但无 task start 事件——大概率 SIGABRT 伪装成功，看 output_tail。"
        elif code == -1073741510:
            payload["summary"] = "进程被控制台关闭/Ctrl+C 终止（0xC000013A）。"
        elif code is None and (self.state.runner.dir / "stop-flag.txt").is_file():
            payload["summary"] = "任务已被 stop_task 停止（watcher 整树终止）。"
        else:
            payload["summary"] = f"MaaPiCli 已退出，退出码 {code}：看 output_tail 与运行目录日志定位原因。"
        self.state.fails.observe(
            running=running,
            failed=bool(events.get("failed")),
            succeeded=bool(events.get("succeeded")),
        )
        hint = self.state.fails.take_suggestion()
        if hint:
            payload["demotion_hint"] = hint
        payload["mode"] = "m2"
        return Ok(payload)

    @plugin_entry(
        id="ef_stop_task",
        name="停止任务",
        description="终止 MaaPiCli 运行器进程（任务立即停止；游戏本身不受影响）。",
        timeout=30.0,
    )
    async def stop_task(self, **_):
        # M6：停 GameScaling 代理循环（内部会抬起全部按键）
        if self.state.m6 is not None:
            if self.state.m6.running:
                res = await self.state.m6.stop("ef_stop_task")
                self.state.mode = ""
                return Ok({"stopped": bool(res.get("stopped")), "mode": "m6",
                           "note": f"GameScaling 代理已停（{res.get('reason')}），按键已全部抬起。"})
            self.state.m6 = None
        # M3：停 NitroGen 代理并还原全局套组模式
        if self.state.m3_active:
            suite = self._suite_client(await self._current_settings())
            stopped = await suite.stop_agent()
            back = await exit_nitrogen(suite)
            self.state.m3_active = False
            self.state.mode = ""
            note = "NitroGen 代理已停"
            if back.get("error"):
                note += f"；但切回 decision 失败：{back['error']}（手动 nitrogen 面板处理）"
            else:
                note += "，套组模式已切回 decision。"
            return Ok({"stopped": not stopped.get("error"), "mode": "m3", "note": note})
        # M1：委托控制服务整树终止
        if self.state.mode == "m1":
            control = await self._control_client()
            if control is not None:
                try:
                    res = await asyncio.to_thread(control.stop)
                    self.state.mode = ""
                    return Ok({"stopped": bool(res.get("stopped")), "mode": "m1"})
                except ControlError as exc:
                    return Err(SdkError(f"控制服务停止失败：{exc}"))
        if self.state.runner is None:
            return Err(SdkError("没有正在管理的任务。"))
        stopped = self.state.runner.stop()
        self.state.mode = ""
        return Ok(
            {
                "stopped": stopped,
                "mode": "m2",
                "note": "已终止" if stopped else "进程已不在运行。",
            }
        )

    @plugin_entry(
        id="ef_modes",
        name="执行模式阶梯",
        description="七模式 probe 汇总（DESIGN-maaend-suite-modes.md）：M0 只读陪伴 / "
        "M1 MaaEndControl 控制服务 / M2 内置 PiRunner / M3 NitroGen 视觉代理 / "
        "M4 人工辅助 / M5 操作回放（operation_learner，仅显式进入）/ "
        "M6 GameScaling 视觉语言代理（VLM 键鼠，仅显式进入）。返回各模式可用性、"
        "当前选中与原因、失败计数，以及 MaaEnd 无效时的替代提议（M3 需用户确认）。"
        "只读，不拉起任何进程。",
        llm_result_fields=["summary", "chosen", "reason", "modes"],
        timeout=60.0,
    )
    async def modes(self, **_):
        settings = await self._current_settings()
        control = await self._control_client()

        def _health():
            return control.health() if control is not None else None

        install = await self._install()
        payload = await probe_modes(
            settings=settings,
            control_health=_health,
            install_probe=lambda: install,
            suite=self._suite_client(settings),
            active_mode=self.state.mode,
            fail_streak=self.state.fails.streak,
            m5_probe=self._m5_probe,
            m6_probe=self._m6_probe,
        )
        usable = "、".join(m["mode"] for m in payload["modes"] if m.get("available"))
        payload["summary"] = (
            f"当前模式 {self.state.mode or '（未执行过，选中 ' + payload['chosen'] + '）'}；"
            f"原因：{payload['reason']}；可用：{usable or '仅 M0/M4'}。"
        )
        return Ok(payload)

    async def _m5_probe(self) -> dict[str, Any]:
        """M5 可用性探测：operation_learner 在线且 learner_status 可达（约定式入口）。"""
        settings = await self._current_settings()
        res = await self._suite_client(settings).call(
            "operation_learner", "learner_status", {}, timeout=5.0)
        if res.get("error"):
            return {"available": False,
                    "reason": f"operation_learner 不可用：{str(res['error'])[:120]}"}
        return {
            "available": True,
            "detail": {
                "mode": res.get("mode"),
                "running": res.get("running"),
                "hooks_running": res.get("hooks_running"),
            },
        }

    async def _m6_probe(self) -> dict[str, Any]:
        """M6 可用性探测：闸门开 + vLLM 服务在线 + 提示词文件在（只读，不 spawn）。"""
        settings = await self._current_settings()
        if not settings.get("gamescaling_enabled"):
            return {"available": False,
                    "reason": "未启用（maa.gamescaling_enabled=false，M6 默认关）"}
        from ..backends.agent import policy_config_from_settings
        from ..backends.gamescaling import GameScalingPolicy

        cfg = policy_config_from_settings(settings)
        prompt = self.data_path("gamescaling") / str(
            settings.get("gamescaling_prompt") or "endfield_system_prompt.txt")
        detail: dict[str, Any] = {"url": cfg.base_url, "model": cfg.model,
                                  "reasoning": cfg.reasoning, "prompt": str(prompt)}
        if not prompt.is_file():
            return {"available": False,
                    "reason": f"提示词文件缺失：{prompt}", "detail": detail}
        healthy = await asyncio.to_thread(GameScalingPolicy("", cfg).health_check, 2.0)
        if not healthy:
            return {"available": False,
                    "reason": f"vLLM 服务不在线（{cfg.base_url}，先起服务）",
                    "detail": detail}
        return {"available": True, "detail": detail}

    @plugin_entry(
        id="ef_replay",
        name="操作回放（M5）",
        description="M5：调 operation_learner 回放「示教/学习」到的操作（跨插件调用）。"
        "dry_run=true（默认）只校验预演、不注入输入；dry_run=false 会真实注入输入——"
        "需 maa.allow_run=true 且 confirm=true（与 M1/M2/M3 同级闸门）。",
        timeout=90.0,
    )
    async def replay(
        self,
        record_id: Annotated[str, "按记录 id 回放（空 = 按场景+意图检索）"] = "",
        scene_id: Annotated[str, "场景 id（空 = 用 operation_learner 当前感知）"] = "",
        intent: Annotated[str, "意图关键词（检索用）"] = "",
        precision: Annotated[str, "auto / coarse / fine"] = "auto",
        dry_run: Annotated[bool, "true = 只校验预演不注入（默认）"] = True,
        confirm: Annotated[bool, "非 dry_run 必填 true（用户明确同意注入）"] = False,
        **_,
    ):
        if not dry_run:
            gate = await self._gate_run()
            if gate is not None:
                return gate
            if not confirm:
                return Err(SdkError(
                    "非 dry_run 的 M5 回放会真实注入输入（SendInput）："
                    "确认后带 confirm=true 重试。"))
        settings = await self._current_settings()
        res = await self._suite_client(settings).call(
            "operation_learner", "ol_execute",
            {"record_id": str(record_id or ""), "scene_id": str(scene_id or ""),
             "intent": str(intent or ""), "precision": str(precision or "auto"),
             "dry_run": bool(dry_run)},
            timeout=75.0,
        )
        if res.get("error"):
            return Err(SdkError(f"M5 回放失败：{res['error']}"))
        return Ok({"m5": res, "dry_run": bool(dry_run)})

    @plugin_entry(
        id="ef_agent",
        name="视觉代理接管",
        description="M3：切换套组模式到 nitrogen 并启动 NitroGen 视觉代理接管游戏"
        "（通用但无完成保证，进度靠画面人肉判断）。危险操作：confirm 必须为 true"
        "（用户明确同意）；目标进程默认 maa.nitrogen_target_process。"
        "结束后用 ef_stop_task 还原（停代理 + 切回 decision）。",
        timeout=60.0,
    )
    async def start_agent(
        self,
        confirm: Annotated[bool, "用户明确同意接管手柄才能 true"] = False,
        target_process: Annotated[str, "目标进程名（默认 maa.nitrogen_target_process）"] = "",
        force: Annotated[bool, "套组互斥覆盖：有同伴在玩也强行启动（默认否）"] = False,
        **_,
    ):
        gate = await self._gate_run()
        if gate is not None:
            return gate
        if not confirm:
            return Err(
                SdkError(
                    "NitroGen 接管是危险操作：会切换全局套组模式并接管手柄，且无完成保证。"
                    "经用户明确同意后带 confirm=true 重试。"
                )
            )
        settings = await self._current_settings()
        process = (target_process or "").strip() or str(
            settings.get("nitrogen_target_process") or "Endfield.exe"
        )
        suite = self._suite_client(settings)
        result = await enter_nitrogen(suite, process, force=force)
        if result.get("error"):
            return Err(SdkError(str(result["error"])))
        self.state.m3_active = True
        self.state.mode = "m3"
        return Ok(
            {
                "started": True,
                "mode": "m3",
                "target_process": process,
                "note": "NitroGen 已接管（无完成保证）：画面由 nitrogen_agent 陪伴流自动推送；"
                "停止用 ef_stop_task（会停代理并把套组模式切回 decision）。",
            }
        )
