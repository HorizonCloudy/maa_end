"""maa_end 执行器链 —— 模式阶梯 M0-M6 的调度核心（DESIGN-maaend-suite-modes.md §3/§4）。

纯调度逻辑与后端解耦，可直接单测：

- choose_mode()：策略矩阵（闸门 → M1 → M2 → M0），M3/M4/M5/M6 永不自动选中——
  M3/M4 是「提议目标」（M3 切全局 suite_mode，需确认），M5（操作回放，
  operation_learner 跨插件能力）只能经 ef_replay 显式进入，M6（GameScaling
  VLM 键鼠代理）只能经 ef_gamescaling 显式进入；
- FailTracker：连续失败计数，达到阈值只「建议降级」，不自动切；
- enter_nitrogen()/exit_nitrogen()：M3 的进入/退出（suite_mode 切换 + 启停代理），
  confirm 语义由调用方持有——router 只在用户明确同意后传 confirm=True。

后端 probe 注入（避免循环导入）：M1 = ControlClient.health（router 注入 callable），
M2 = find_install（router 注入 callable），M3 = SuiteClient（本模块直接消费）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

MODE_LABELS = {
    "m0": "只读陪伴（YOLO+事件流+提醒）",
    "m1": "MaaEndControl（本地控制服务）",
    "m2": "内置 PiRunner（MaaPiCli 直跑）",
    "m3": "NitroGen 通用视觉代理（接管手柄，无完成保证）",
    "m4": "人工辅助（只出计划/提醒，不执行）",
    "m5": "操作回放（operation_learner 示教→安全重放，仅显式进入）",
    "m6": "GameScaling 视觉语言代理（VLM 直接键鼠，无完成保证）",
}


# ---------------------------------------------------------------------------
# 策略矩阵（纯函数，单测友好）
# ---------------------------------------------------------------------------

def choose_mode(*, allow_run: bool = True, m1_ok: bool = False, m2_ok: bool = False) -> tuple[str, str]:
    """返回 (mode, reason)。M3/M4 不出现——它们只能经 ef_agent/ef_remind 显式进入。"""
    if not allow_run:
        return "m0", "闸门未放行（maa.allow_run=false）→ 只读模式"
    if m1_ok:
        return "m1", "MaaEndControl 在线"
    if m2_ok:
        return "m2", "MaaEndControl 缺席，回退内置 PiRunner"
    return "m0", "MaaEnd 不可用（未安装/损坏）：可提议 M3（需确认）或安装指引"


# ---------------------------------------------------------------------------
# 连续失败跟踪（只建议，不自动降级）
# ---------------------------------------------------------------------------

class FailTracker:
    """task_failed 事件连续出现计数；达到阈值置 demote（调用方追加建议文案）。"""

    def __init__(self, threshold: int = 2) -> None:
        self.threshold = max(1, int(threshold))
        self.streak = 0
        self.suggested = False  # 同一轮失败只建议一次，成功/重启后复位

    def observe(self, *, running: bool, failed: bool, succeeded: bool) -> None:
        if running:
            return
        if failed:
            self.streak += 1
        elif succeeded:
            self.streak = 0
            self.suggested = False

    @property
    def demote(self) -> bool:
        return self.streak >= self.threshold

    def take_suggestion(self) -> str | None:
        """若应建议降级且未建议过，返回建议文案并标记。"""
        if self.demote and not self.suggested:
            self.suggested = True
            return (
                f"任务已连续失败 {self.streak} 次：可重试/换任务（ef_list_tasks），"
                "或确认后切 NitroGen 视觉代理（ef_agent confirm=true），或只读模式看着。"
            )
        return None

    def reset(self) -> None:
        self.streak = 0
        self.suggested = False


# ---------------------------------------------------------------------------
# M3 NitroGen 进入/退出（SuiteClient 已有 call 通道，这里补语义封装）
# ---------------------------------------------------------------------------

async def enter_nitrogen(
    suite: Any,
    target_process: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """切 suite_mode→nitrogen（confirm=True 必带：调用方已取得用户同意）→ 启动代理。

    返回 start_game_agent 的结果；任一步失败原样带 error 返回（suite.call 不抛）。
    注意：若 start 失败，suite_mode 已切走——调用方应调 exit_nitrogen 还原。
    """
    switch = await suite.set_suite_mode("nitrogen", confirm=True)
    if switch.get("error"):
        return {"error": f"切换套组模式失败：{switch['error']}", "step": "set_suite_mode"}
    started = await suite.start_agent(target_process, force=force)
    if started.get("error"):
        # 尽力还原，避免套组卡在 nitrogen 模式
        await exit_nitrogen(suite)
        return {"error": f"启动 NitroGen 代理失败：{started['error']}", "step": "start_game_agent"}
    return started


async def exit_nitrogen(suite: Any) -> dict[str, Any]:
    """停代理 + 切回 decision（nitrogen_agent 语义：切回无条件并自动停代理）。"""
    back = await suite.set_suite_mode("decision", confirm=False)
    return back


# ---------------------------------------------------------------------------
# 模式探测汇总（ef_modes 入口用）
# ---------------------------------------------------------------------------

async def probe_modes(
    *,
    settings: dict[str, Any],
    control_health: Callable[[], dict | None],
    install_probe: Callable[[], dict[str, Any]],
    suite: Any,
    active_mode: str = "",
    fail_streak: int = 0,
    m5_probe: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    m6_probe: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """七模式 probe 汇总（只读；control_health 不 spawn；m5/m6_probe 由 router 注入）。"""
    allow_run = bool(settings.get("allow_run"))
    m1 = control_health() or {}
    m1_ok = bool(m1.get("ok")) and bool(m1.get("runner_ready"))
    install = install_probe() or {}
    m2_ok = bool(install.get("found")) and bool(install.get("runtime_ready"))

    m3: dict[str, Any] = {"mode": "m3", "label": MODE_LABELS["m3"], "available": False}
    layer_on = await suite.layer_enabled()
    if layer_on:
        st = await suite.nitrogen_status()
        arb = await suite.suite_status(probe=False)
        m3["available"] = not st.get("error")
        m3["suite_mode"] = str(arb.get("mode") or "?")
        m3["agent_running"] = bool((st.get("agent") or {}).get("running"))
        if st.get("error"):
            m3["reason"] = str(st["error"])

    m5: dict[str, Any] = {"mode": "m5", "label": MODE_LABELS["m5"], "available": False,
                          "reason": "未提供 M5 探测（operation_learner 未接入）"}
    if m5_probe is not None:
        try:
            probed = await m5_probe()
        except Exception as exc:  # noqa: BLE001 - 探测失败如实报，不抛出
            probed = {"available": False, "reason": f"探测失败：{type(exc).__name__}: {exc}"}
        if isinstance(probed, dict):
            m5.update(probed)
        m5["available"] = bool(m5.get("available"))

    m6: dict[str, Any] = {"mode": "m6", "label": MODE_LABELS["m6"], "available": False,
                          "reason": "未提供 M6 探测（GameScaling 未启用）"}
    if m6_probe is not None:
        try:
            probed = await m6_probe()
        except Exception as exc:  # noqa: BLE001 - 探测失败如实报，不抛出
            probed = {"available": False, "reason": f"探测失败：{type(exc).__name__}: {exc}"}
        if isinstance(probed, dict):
            m6.update(probed)
        m6["available"] = bool(m6.get("available"))

    mode, reason = choose_mode(allow_run=allow_run, m1_ok=m1_ok, m2_ok=m2_ok)
    return {
        "active_mode": active_mode or mode,
        "chosen": mode,
        "reason": reason,
        "fail_streak": int(fail_streak),
        "modes": [
            {"mode": "m0", "label": MODE_LABELS["m0"], "available": True},
            {"mode": "m1", "label": MODE_LABELS["m1"], "available": m1_ok,
             "detail": {"url": settings.get("control_url") or "",
                        "version": m1.get("version", ""), "prepared": m1.get("prepared")},
             **({} if m1_ok else {"reason": "控制服务不在线或 fork 未部署"})},
            {"mode": "m2", "label": MODE_LABELS["m2"], "available": m2_ok,
             **({} if m2_ok else {"reason": "MaaEnd 未安装或运行库不齐"})},
            m3,
            {"mode": "m4", "label": MODE_LABELS["m4"], "available": True},
            m5,
            m6,
        ],
    }
