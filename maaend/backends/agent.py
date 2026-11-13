"""maa_end GameScaling 代理循环 —— M6 的调度核心（截图 → VLM 决策 → 键鼠注入）。

与 executor.py 的分工同款：本模块管「跑起来 / 停下来 / 汇报状态」，闸门与
confirm 语义由 router 持有（ef_gamescaling 过 gamescaling_enabled + allow_run +
confirm 三重闸门后才调 start）。

循环每轮：
  capture_window(hwnd)         → (w, h, BGR)，内含置前台（SendInput 落点保证）
  policy.act_bgr(w, h, bgr)    → 一次 200ms 决策（vLLM，可思考）
  io.execute_chunk(plan)       → 6 × 33ms 真实键鼠事件

推理耗时叠加在 200ms 之上（思考模式可达秒级）——块间空转是方案固有形态，
不是缺陷；需要跟手时关 reasoning（gamescaling_reasoning=false）。

熔断（自动停 + release_all 防卡键）：
  - 游戏窗口消失（capture 抛错）；
  - 连续 N 次请求失败（gamescaling_max_consecutive_errors，默认 5）；
  - 跑满 gamescaling_max_chunks 块（0 = 不限）；
  - ef_gamescaling action=stop / ef_stop_task / 插件卸载（on_shutdown）。

阻塞调用（capture/HTTP/execute）全部经 asyncio.to_thread；状态字典随时可读。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

try:  # 插件内按包加载（maaend.gamescaling_agent）
    from ..vision.capture import CaptureError as YoloError
    from ..vision.capture import capture_window
    from . import input_io as gsio
    from .gamescaling import GameScalingPolicy, PolicyConfig, probe_encoder
except ImportError:  # 纯逻辑单测按顶层模块加载（sys.path 挂 maaend/）
    from backends import input_io as gsio
    from backends.gamescaling import GameScalingPolicy, PolicyConfig, probe_encoder
    from vision.capture import CaptureError as YoloError
    from vision.capture import capture_window


class AgentError(RuntimeError):
    """启动前置不满足 / 循环异常。"""


def policy_config_from_settings(settings: dict[str, Any]) -> PolicyConfig:
    """[maa] 配置段 → PolicyConfig（gamescaling_* 键）。纯函数。"""
    cfg = PolicyConfig()
    cfg.base_url = str(settings.get("gamescaling_url") or cfg.base_url).rstrip("/")
    cfg.model = str(settings.get("gamescaling_model") or cfg.model)
    cfg.reasoning = bool(settings.get("gamescaling_reasoning", True))
    try:
        cfg.history_len = max(0, int(settings.get("gamescaling_history_len") or 5))
    except (TypeError, ValueError):
        cfg.history_len = 5
    return cfg


class GamescalingAgent:
    """单实例代理。start 幂等拒绝；stop 可重复调用；status 永不抛。"""

    def __init__(self, log: Callable[[str], None] | None = None) -> None:
        self._log = log or (lambda msg: None)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._policy: GameScalingPolicy | None = None
        # ---- 状态（status() 直接读，循环只在这些键上写）----
        self.started_at = 0.0
        self.stop_reason = ""
        self.chunks = 0
        self.parsed_count = 0
        self.error_count = 0
        self.consecutive_errors = 0
        self.latency_total_ms = 0.0
        self.latency_max_ms = 0.0
        self.recent: deque[dict[str, Any]] = deque(maxlen=20)
        self.window: dict[str, Any] = {}
        self.encoder = ""

    # ------------------------------------------------------------ 生命周期

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(
        self,
        *,
        settings: dict[str, Any],
        window: dict[str, Any],
        prompt_path: Path,
    ) -> dict[str, Any]:
        """启动前置校验 + 起循环。返回状态片段；不满足抛 AgentError。"""
        if self.running:
            raise AgentError("GameScaling 代理已在运行（先 action=stop）。")
        hwnd = int(window.get("hwnd") or 0)
        if not hwnd or not window.get("found"):
            raise AgentError("游戏窗口未找到（先开游戏，或在配置里调 window_*_regex）。")
        if not prompt_path.is_file():
            raise AgentError(
                f"系统提示词文件不存在：{prompt_path}"
                "（data/gamescaling/ 下放一份，文件名对应 gamescaling_prompt）。")
        system_prompt = prompt_path.read_text(encoding="utf-8").strip()
        if "<|action_start|>" not in system_prompt:
            raise AgentError("提示词文件缺少 Output Format 段（须含 <|action_start|>），"
                             "不要手写，从模板生成（见 docs/maaend.md M6 节）。")
        cfg = policy_config_from_settings(settings)
        policy = GameScalingPolicy(system_prompt, cfg)
        await asyncio.to_thread(gsio.check_vocab)
        if not await asyncio.to_thread(policy.health_check, 3.0):
            raise AgentError(f"vLLM 服务不可达：{cfg.base_url}/models"
                             "（先起服务：bash scripts/serve_vllm.sh，见 GameScaling README）。")
        try:
            self.encoder = await asyncio.to_thread(probe_encoder, cfg)
        except ValueError as exc:
            raise AgentError(f"帧编码自检失败：{exc}") from exc

        # ---- 全部前置过了，才落到实例状态并起任务 ----
        self._policy = policy
        self.window = dict(window)
        self._stop = asyncio.Event()
        self.stop_reason = ""
        self.chunks = self.parsed_count = self.error_count = self.consecutive_errors = 0
        self.latency_total_ms = self.latency_max_ms = 0.0
        self.recent.clear()
        self.started_at = time.time()
        policy.reset()
        max_chunks = _to_int(settings.get("gamescaling_max_chunks"), 0)
        max_errors = _to_int(settings.get("gamescaling_max_consecutive_errors"), 5)
        self._task = asyncio.create_task(
            self._loop(hwnd, max_chunks, max_errors))
        self._log(f"M6 GameScaling 代理已启动（hwnd={hwnd}，encoder={self.encoder}）")
        return {"started": True, "hwnd": hwnd, "encoder": self.encoder,
                "base_url": cfg.base_url, "model": cfg.model,
                "reasoning": cfg.reasoning, "max_chunks": max_chunks}

    async def stop(self, reason: str = "手动停止") -> dict[str, Any]:
        """置停止位 → 等循环退出 → 抬起全部键。幂等，可从 on_shutdown 调。"""
        if not self.running:
            if self._task is not None:  # 刚退出的任务也等一下，确保 release 完成
                try:
                    await self._task
                except Exception:  # noqa: BLE001 - 停止路径不因循环异常失败
                    pass
                self._task = None
            if not self.stop_reason:
                self.stop_reason = reason
            return {"stopped": False, "reason": "未在运行", "stop_reason": self.stop_reason}
        self.stop_reason = reason
        self._stop.set()
        try:
            await self._task
        except Exception as exc:  # noqa: BLE001 - 如实记原因，不向上抛
            self._log(f"M6 循环异常退出：{type(exc).__name__}: {exc}")
            self.stop_reason = f"循环异常：{type(exc).__name__}: {exc}"
        self._task = None
        return {"stopped": True, "reason": reason, "chunks": self.chunks}

    # ------------------------------------------------------------ 循环

    async def _loop(self, hwnd: int, max_chunks: int, max_errors: int) -> None:
        assert self._policy is not None
        try:
            while not self._stop.is_set():
                try:
                    width, height, bgr = await asyncio.to_thread(capture_window, hwnd)
                except (YoloError, OSError) as exc:
                    await self._auto_stop(f"游戏窗口截图失败（{type(exc).__name__}: {exc}）")
                    return
                decision = await asyncio.to_thread(self._policy.act_bgr, width, height, bgr)
                self._record(decision)
                if decision.error:
                    self.consecutive_errors += 1
                    if max_errors > 0 and self.consecutive_errors >= max_errors:
                        await self._auto_stop(
                            f"连续 {self.consecutive_errors} 次请求失败：{decision.error}")
                        return
                    await asyncio.sleep(0.2)  # 失败退避，别热转
                    continue
                self.consecutive_errors = 0
                plans = gsio.plan_chunk(decision.action, width, height)
                await asyncio.to_thread(gsio.execute_chunk, plans)
                self.chunks += 1
                if max_chunks > 0 and self.chunks >= max_chunks:
                    await self._auto_stop(f"跑满 max_chunks={max_chunks} 块")
                    return
            if not self.stop_reason:
                self.stop_reason = "手动停止"
        finally:
            try:  # 无论哪条路径退出，键全部抬起（execute_chunk 块末已抬，这里是兜底）
                await asyncio.to_thread(gsio.release_all)
            except Exception as exc:  # noqa: BLE001 - 兜底失败要如实可见
                self._log(f"M6 release_all 失败：{exc}")

    async def _auto_stop(self, reason: str) -> None:
        self._log(f"M6 自动熔断：{reason}")
        self.stop_reason = reason
        self._stop.set()  # 循环由调用方 return；release_all 走 finally

    def _record(self, decision: Any) -> None:
        if decision.error:
            self.error_count += 1
        elif decision.action.parsed:
            self.parsed_count += 1
        self.latency_total_ms += decision.latency_ms
        self.latency_max_ms = max(self.latency_max_ms, decision.latency_ms)
        self.recent.append({
            "t": round(time.time(), 3),
            "latency_ms": round(decision.latency_ms, 1),
            "parsed": bool(decision.action.parsed),
            "error": decision.error,
            "action": decision.action.to_text() if decision.action.parsed else "",
            "reasoning_head": (decision.reasoning or "")[:200],
        })

    # ------------------------------------------------------------ 状态

    def status(self) -> dict[str, Any]:
        decisions = self.parsed_count + self.error_count
        avg = self.latency_total_ms / decisions if decisions else None
        return {
            "running": self.running,
            "started_at": self.started_at or None,
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else None,
            "stop_reason": self.stop_reason or None,
            "window": self.window or None,
            "encoder": self.encoder or None,
            "chunks": self.chunks,
            "decisions": decisions,
            "parsed_count": self.parsed_count,
            "error_count": self.error_count,
            "consecutive_errors": self.consecutive_errors,
            "avg_latency_ms": round(avg, 1) if avg is not None else None,
            "max_latency_ms": round(self.latency_max_ms, 1) or None,
            "parse_rate": round(self.parsed_count / decisions, 3) if decisions else None,
            "recent": list(self.recent)[-5:],
        }


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
