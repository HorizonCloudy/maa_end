"""提醒循环、战斗报告、ZMDLogs 状态、每日编排。

由 `maaend/router.py` 拆分而来（2026-11-04，见 docs/REFACTOR-PLAN.md 阶段 3）。
状态一律经 `self.state`（AppState）读写，不直接持有实例字段。
"""

from __future__ import annotations

import asyncio
import os
from typing import Annotated, Any

from plugin.sdk.plugin import (
    Err,
    Ok,
    SdkError,
    llm_tool,
    plugin_entry,
    ui,
)

from ..infra.probe import (
    foreground_hwnd,
)
from ..report import generate_report
from ._helpers import game_window


class OpsAPI:
    """提醒循环、战斗报告、ZMDLogs 状态、每日编排。"""
    async def _remind_loop(self) -> None:
        """每 10s 检查：时间类到点 + 数据流任务事件；每 30s OCR 条件（前台时）。异常永不退出。"""
        tick = 0
        while True:
            await asyncio.sleep(10)
            tick += 1
            try:
                self._fire_reminders()
            except Exception as exc:  # noqa: BLE001 - 提醒循环永不拖垮插件
                self._log("warning", f"提醒检查异常（忽略继续）：{exc}")
            if tick % 3 == 0:
                try:
                    await self._fire_ocr_reminders()
                except Exception as exc:  # noqa: BLE001
                    self._log("warning", f"OCR 条件检查异常（忽略继续）：{exc}")

    async def _fire_ocr_reminders(self) -> None:
        """ocr_text 条件提醒：只在有该类提醒待触发且游戏窗口前台时读屏（不抢焦点）。"""
        if not any(r.kind == "ocr_text" for r in self.state.reminders.pending()):
            return
        detector = self.state.detector
        if detector is None:
            if not self.state.ocr_warned:
                self.state.ocr_warned = True
                self._log("warning", "视觉 sidecar 未启动，ocr_text 提醒无法触发")
            return
        settings = await self._current_settings()
        win = game_window(settings)
        if not win.get("found") or foreground_hwnd() != int(win.get("hwnd") or 0):
            return  # 游戏不在前台：跳过本轮（截图会抢焦点），等用户回游戏再看
        try:
            resp = await asyncio.to_thread(detector.ocr_hwnd, int(win["hwnd"]))
        except Exception as exc:  # noqa: BLE001
            if not self.state.ocr_warned:
                self.state.ocr_warned = True
                self._log("warning", f"OCR 轮询失败（sidecar 缺 rapidocr？）：{str(exc)[:120]}")
            return
        if not resp.get("ok"):
            if not self.state.ocr_warned:
                self.state.ocr_warned = True
                self._log("warning", f"OCR 轮询被拒：{str(resp.get('error'))[:120]}")
            return
        self.state.ocr_warned = False
        for r in self.state.reminders.observe_ocr(str(resp.get("full_text") or "")):
            self._push_reminder(self.state.reminders.compose(r, None))

    def _fire_reminders(self) -> None:
        fired: list[tuple[Any, dict | None]] = [(r, None) for r in self.state.reminders.due()]
        if self.state.stream is not None:
            entries = self.state.stream.store.read(
                category="tasks", since=self.state.remind_cursor, limit=50)
            self.state.remind_cursor = self.state.stream.store.latest_id()
            fired.extend(self.state.reminders.observe(entries))
        for reminder, event in fired:
            self._push_reminder(self.state.reminders.compose(reminder, event))

    def _push_reminder(self, text: str) -> None:
        """投递提醒；push_message 只能在宿主认可的上下文调用，失败降日志不抛。"""
        sender = None
        mp = getattr(self, "main_plugin", None)
        if mp is not None and hasattr(mp, "push_message"):
            sender = mp.push_message
        elif hasattr(self, "push_message"):
            sender = self.push_message
        if sender is None:
            self._log("info", f"[提醒·仅日志] {text}")
            return
        try:
            res = sender(
                source="maa_end_remind",
                visibility=["chat"],
                ai_behavior="read",
                parts=[{"type": "text", "text": text}],
            )
            if isinstance(res, dict) and not res.get("submitted"):
                self._log("warning", f"提醒投递失败（{res.get('reason')}），仅日志：{text}")
        except Exception as exc:  # noqa: BLE001 - RuntimeError（上下文）或传输失败
            self._log("warning", f"提醒投递异常（仅日志）：{exc}｜{text}")

    @plugin_entry(
        id="ef_remind",
        name="设置提醒",
        description="M4 人工辅助：设一个提醒到点推送。kind=after_task（任务结束触发，"
        "value 填任务 entry 或留空=任意）、at_time（每天 HH:MM）、in_minutes（N 分钟后）。"
        "会话内存态：插件重载后清空（ef_reminders 可查/可取消）。",
        timeout=15.0,
    )
    async def remind(
        self,
        kind: Annotated[str, "after_task / at_time / in_minutes"] = "in_minutes",
        value: Annotated[str, "after_task=任务entry(空=任意)；at_time=HH:MM；in_minutes=分钟数"] = "30",
        message: Annotated[str, "提醒附言（可空）"] = "",
        **_,
    ):
        try:
            r = self.state.reminders.add(kind, value, message)
        except ValueError as exc:
            return Err(SdkError(str(exc)))
        return Ok({
            "created": True,
            "id": r.id, "kind": r.kind, "value": r.value,
            "due_at": round(r.due_at, 3) if r.due_at else None,
            "message": r.message,
            "note": "到点经 push_message 推送到对话（ai_behavior=read）。",
        })

    @plugin_entry(
        id="ef_reminders",
        name="提醒清单",
        description="查看/取消 M4 提醒。action=list（默认，含已触发历史）；"
        "action=cancel 时给 id 取消。",
        timeout=15.0,
    )
    async def reminders(
        self,
        action: Annotated[str, "list / cancel"] = "list",
        id: Annotated[str, "取消时给提醒 id（如 r1）"] = "",
        **_,
    ):
        if str(action) == "cancel":
            ok = self.state.reminders.cancel(str(id or ""))
            return Ok({"cancelled": ok, "id": str(id or ""),
                       "note": "已取消" if ok else "没有这个提醒（可能已触发或已取消）。"})
        pending = self.state.reminders.pending()
        return Ok({
            "total": len(self.state.reminders.to_dicts()),
            "pending": len(pending),
            "items": self.state.reminders.to_dicts(),
        })

    @plugin_entry(
        id="ef_report",
        name="战斗复盘",
        description="读 ZMDLogs 最近 N 个 session 事件流，生成对局 Markdown 复盘报告"
        "（战斗数 / 时长 / 伤害 / 阵亡 / 技能 / 失败判定与归因）。"
        "只读：不订阅、不注入、不写盘。",
        llm_result_fields=["summary", "battles", "report"],
        timeout=120.0,
    )
    async def battle_report(
        self,
        sessions: Annotated[int, "读取最近的 session 文件数（默认 1）"] = 1,
        max_battles: Annotated[int, "最多分析的战斗场数（默认 50，保留最近）"] = 50,
        **_,
    ):
        settings = await self._current_settings()
        result = await asyncio.to_thread(
            generate_report,
            log_dir=str(settings.get("assist_log_dir") or ""),
            sessions=max(1, int(sessions or 1)),
            max_battles=max(1, int(max_battles or 50)),
            consecutive_fail_threshold=int(
                settings.get("consecutive_fail_threshold") or 3),
        )
        if not result.get("ok"):
            return Err(SdkError(str(result.get("error") or "复盘失败")))
        result["summary"] = (
            f"{result['battles']} 场已复盘（数据源：{len(result['files'])} 个 session 文件）"
        )
        return Ok(result)

    @ui.action(id="ef_zmdlogs_status", label="战斗事件流状态")
    @plugin_entry(
        id="ef_zmdlogs_status",
        name="战斗事件流状态",
        description="ZMDLogs 战斗事件流（伤害/连携/buff）的接入状态：闸门、源码目录、Npcap、"
        "RSA 私钥、子进程存活、WS 端口、最近 stderr。卡关辅助没有数据或事件流起不来时先跑这个。",
        llm_result_fields=["summary", "enabled", "running", "ws_open", "ready", "blocking"],
        timeout=30.0,
    )
    async def zmdlogs_status(self, action: str = "status", **_):
        """`action=status` 只看不改；`action=start` 显式拉起子进程（仍受闸门约束）。"""
        settings = await self._current_settings()
        runner = self._zmd_client(settings)
        act = str(action or "status").strip().lower()

        if act in ("start", "启动", "on"):
            if not runner.enabled:
                return Err("zmdlogs_enabled=false，事件流闸门关闭（它要在网卡层抓包，默认不开）")
            result = runner.start()
            if not result.get("ok"):
                return Err(str(result.get("error") or "启动失败"))
            await asyncio.sleep(0.6)  # 给它一点时间绑端口，否则 ws_open 必假
            acted = "已按请求拉起"
        elif act in ("stop", "停止", "off"):
            stopped = runner.stop()
            acted = "已按请求停掉" if stopped.get("was_running") else "本来就没在运行"
        elif act != "status":
            return Err("action 只能是 status / start / stop")
        else:
            acted = ""

        state = runner.state()
        pf = state.get("preflight") or {}
        blocking = list(pf.get("blocking") or [])
        mode = state.get("launch_mode") or pf.get("launch_mode") or ""
        if state["running"] and not state["ws_open"]:
            blocking.append("子进程已拉起但 WS 端口未监听（可能仍在加载，或已异常退出）")

        mode_tag = f"[{mode} 模式]" if mode else ""
        if not state["enabled"]:
            summary = "战斗事件流闸门关闭（zmdlogs_enabled=false）。卡关辅助只能读 logs 目录里的历史事件。"
        elif state.get("external"):
            summary = (f"{mode_tag}事件流可用（复用已在运行的实例）：ws={state['ws_url']} "
                       "端口在监听，卡关辅助可实时订阅。")
        elif not state["running"]:
            summary = (f"{mode_tag}事件流未运行。" + ("阻塞：" + "；".join(blocking) if blocking
                                        else "需要时用 action=start 拉起。"))
        elif state["ws_open"]:
            summary = (f"{mode_tag}事件流运行中：pid={state['pid']} ws={state['ws_url']} "
                       f"已运行 {state['uptime_seconds']}s，卡关辅助可实时订阅。")
        else:
            summary = f"{mode_tag}事件流已拉起，WS 尚未就绪：" + ("；".join(blocking) or "稍候再查。")
        if acted:
            summary = f"{acted}。{summary}"

        return Ok({
            "summary": summary,
            "enabled": state["enabled"],
            "launch_mode": mode,
            "running": state["running"],
            "external": bool(state.get("external")),
            "pid": state["pid"],
            "uptime_seconds": state["uptime_seconds"],
            "ws_url": state["ws_url"],
            "ws_open": state["ws_open"],
            "exit_code": state["exit_code"],
            "log_dir": state["log_dir"],
            "ready": bool(pf.get("ready")),
            "blocking": blocking,
            "log_tail": state["log_tail"][-8:],
            "preflight": pf,
        })

    @llm_tool(name="maa_zmdlogs", description="终末地战斗事件流(ZMDLogs):action=status 查状态(默认);action=start 拉起抓包子进程;action=stop 停掉。需 Npcap+RSA私钥,闸门 zmdlogs_enabled 默认关。事件流提供伤害/连携/buff 实时数据,卡关辅助靠它做实时判定。",
              parameters={"type": "object", "properties": {"action": {"type": "string", "enum": ["status", "start", "stop"]}}})
    async def maa_zmdlogs_tool(self, **kwargs):
        res = await self.zmdlogs_status(action=str(kwargs.get("action") or "status"))
        payload = getattr(res, "data", None)
        if payload is None:
            payload = {"error": str(getattr(res, "message", res))[:200]}
        return {"output": payload, "is_error": bool(getattr(res, "is_error", False))}

    @llm_tool(name="maa_daily", description="每日任务编排器:action=plan 列出今天剩余任务(默认);action=run 后台执行剩余任务并立即返回(需 maa.allow_run 放行,会接管游戏输入;单次 UAC,minutes=时长上限分默认 45,only 可选指定任务名列表;进度再调 plan 看 run 字段);action=reset 清空今日状态。按日期记忆,已完成/配额尽的任务当天自动跳过。跑图/收集/发货等 45 个 MAA 任务统一从这里走。",
              parameters={"type": "object", "properties": {"action": {"type": "string", "enum": ["plan", "run", "reset"]}, "minutes": {"type": "integer", "description": "run 时长上限(分),默认 45"}, "only": {"type": "array", "items": {"type": "string"}, "description": "只跑这些任务名"}}},
              timeout=120.0)
    async def maa_daily_tool(self, **kwargs):
        action = str(kwargs.get("action") or "plan")
        if action not in ("plan", "run", "reset"):
            return {"output": {"error": "action 只能是 plan/run/reset"}, "is_error": True}
        if action == "run":  # 执行类：与 ef_run_task 同一 allow_run 闸门
            gate = await self._gate_run()
            if gate is not None:
                msg = str(getattr(gate.error, "message", gate.error))
                return {"output": {"error": msg}, "is_error": True, "error": msg[:200]}
        try:
            minutes = max(1, int(float(kwargs.get("minutes") or 45)))
        except (TypeError, ValueError):
            minutes = 45
        # 安装根经环境变量传给子进程链（orchestrator → maa_scenes）：配置单一来源
        settings = await self._current_settings()
        env = {**os.environ, "MAAEND_DIR": str(settings.get("maaend_dir") or "")}
        # 检查到 self.state.daily 赋值之间不能有 await，否则并发两次 run 会起两个编排器
        daily_running = self.state.daily is not None and self.state.daily.poll() is None
        if daily_running and action != "plan":
            return {"output": {"error": f"每日编排正在后台运行(pid {self.state.daily.pid});用 action=plan 看进度"},
                    "is_error": True}
        only = kwargs.get("only") or None
        argv = ["-X", "utf8", "daily_orchestrator.py"]
        if action == "plan":
            argv.append("--plan")
        elif action == "reset":
            argv.append("--reset")
        else:
            argv += ["--run", "--minutes", str(minutes)]
            if only:
                if isinstance(only, str):
                    only = [only]
                argv += ["--only", *only]
        import shutil as _sh
        import subprocess  # 本函数内的 _run() 要用；模块级没有导入（第 760 行那处是别的函数内的局部导入，覆盖不到这里）
        exe = _sh.which("python") or "python"
        script = self.data_path("yolo") / "daily_orchestrator.py"
        argv[2] = str(script)
        import traceback as _tb

        log_path = self.data_path("daily_run.log")
        if action == "run":
            # run 最长 minutes 分钟：后台起进程立即返回（工具超时上限 300s），进度用 action=plan 查
            import threading

            try:  # Popen 立即返回，同步调用以保证上面的查重与赋值原子
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "w", encoding="utf-8") as log:  # 子进程继承句柄，父侧可关
                    proc = subprocess.Popen([exe, *argv], stdout=log, stderr=subprocess.STDOUT,
                                            cwd=str(script.parent), env=env)
            except Exception as exc:  # noqa: BLE001
                return {"output": {"error": type(exc).__name__ + ": " + str(exc)[:200]}, "is_error": True}
            self.state.daily = proc

            def _watchdog():  # 编排器自己按 --minutes 收尾；这里只防挂死
                try:
                    proc.wait(timeout=minutes * 60 + 420)
                except subprocess.TimeoutExpired:
                    proc.kill()

            threading.Thread(target=_watchdog, name="maa-daily-watchdog", daemon=True).start()
            return {"output": {"action": "run", "started": True, "pid": proc.pid, "log": str(log_path),
                               "hint": "已在后台运行；用 action=plan 查剩余任务与运行状态"}}

        def _run():
            return subprocess.run([exe, *argv], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  cwd=str(script.parent), timeout=100, env=env)

        try:
            r = await asyncio.wait_for(asyncio.to_thread(_run), timeout=110)
        except Exception as exc:  # noqa: BLE001
            return {"output": {"error": type(exc).__name__ + ": " + str(exc)[:200],
                               "traceback_tail": _tb.format_exc().strip().splitlines()[-3:]},
                    "is_error": True}
        out = (r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr.strip() else "")
        payload = {"returncode": r.returncode, "text": out.strip()[-3000:], "action": action}
        if self.state.daily is not None:  # 后台 run 的进度
            tail = await asyncio.to_thread(
                lambda: log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
                if log_path.is_file() else "")
            payload["run"] = {"running": daily_running, "pid": self.state.daily.pid,
                              "returncode": self.state.daily.poll(), "log_tail": tail}
        return {"output": payload}
