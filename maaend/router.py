"""MaaEnd 终末地桥接 —— N.E.K.O. 原生插件。

MaaEnd（https://github.com/MaaEnd/MaaEnd）是《明日方舟：终末地》的 MAA 助手
桌面应用：MXU 宿主 + MaaFramework 运行库 + Go/C++ AgentServer 子进程。
**它本身不对外提供控制 API**（Go/C++ 子进程是 MaaFramework 的 Agent，随宿主
生灭，见 agent/cpp-algo/source/main.cpp 的 ParentProcessWatcher）。

所以本插件使用 MaaEnd 随包发布的 **MaaPiCli 官方运行器**：按用户选择生成 PI
配置（`config/maa_pi_config.json`），以 `-d` 直通模式执行——完整 PI v2.5.0
语义（选项 case→流水线覆盖、agent 子进程、import 合并）由官方 Runner 负责。
任务清单来自 MaaEnd 的 `interface.json`（MAA 标准格式，任务随发布版本变化），
本插件不写死任何任务名；ctypes 只在 `status` 里读运行库版本号。

安全边界：`run_task` 会接管游戏窗口的截屏与输入，由 `maa.allow_run` 闸门
统一管控（默认关闭）。这只影响本插件；MaaEnd 自己的 GUI 想开就开。

套组分层（MaaEnd/MAA/BetterGI 的【截图→识别→决策→执行】范式在插件套组里的落位）：
识别层 = 本插件 YOLO（yolo.py / ef_yolo，认"是什么"）+ OCR（ocr.py，认"写了
什么"，wiki 本体归类，类名与 YOLO 训练集同名）；决策层 = game_decision 插件
（suite.py 客户端，ef_decide 派单）；执行层 = MaaPiCli 运行器 + NitroGen
游戏代理（nitrogen_agent 插件）；套组仲裁见 suite.py 模块说明。

只依赖标准库（ctypes 是标准库）。宿主是嵌入式 Python，这里没有第三方包。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from plugin.sdk.plugin import (
    Ok,
)

from .._router_base import _GameRouter
from .api.assist import AssistAPI
from .api.install import InstallAPI
from .api.knowledge import KnowledgeAPI
from .api.llm_tools import LLMToolsAPI
from .api.m6 import M6API
from .api.ops import OpsAPI
from .api.task import TaskAPI
from .api.vision import VisionAPI
from .assist import AssistConfig, AssistService
from .backends.control import ControlClient

#: 常量定义在 constants.py：api/ 下的 mixin 也要用，但不能反向 import router（会循环）。
from .constants import CONFIG_PREFIX  # noqa: E402
from .executor import (
    exit_nitrogen,
)
from .infra.probe import (
    count_windows,
    find_install,
)
from .state import AppState
from .suite import SuiteClient
from .zmdlogs import DEFAULT_WS_PORT, ZmdLogsRunner

#: ZMDLogs 日志目录的候选位置在 `assist.LOG_DIR_CANDIDATES`（ZoneReader 每轮
#: poll 懒探测：显式配置 > ZMDLogs 启动后对齐的目录 > 常见位置），router 侧
#: 不再单独维护一份探测逻辑。


class MaaEndRouter(
    InstallAPI,
    TaskAPI,
    AssistAPI,
    VisionAPI,
    KnowledgeAPI,
    OpsAPI,
    M6API,
    LLMToolsAPI,
    _GameRouter,
):
    """把 MaaEnd / MaaFramework 接成 N.E.K.O 的入口与对话工具。"""

    def __init__(self):
        # 必须最先调用：基类 __init__ 末尾会自动注册本类上被 @llm_tool 标记的方法。
        super().__init__(name="maaend")
        # 全部可变状态集中在 AppState（字段语义见 state.py）。路由方法只读写
        # self.state.xxx —— 拆 mixin 时不再依赖彼此的隐藏赋值顺序。
        self.state = AppState()

    def _suite_client(self, settings: dict[str, Any] | None = None) -> SuiteClient:
        if self.state.suite is None:
            s = settings or {}
            self.state.suite = SuiteClient(
                self.plugins,
                layer_id=str(s.get("decision_layer_id") or "game_decision"),
                agent_id=str(s.get("nitrogen_agent_id") or "nitrogen_agent"),
            )
        return self.state.suite

    async def _suite_enabled(self) -> bool:
        settings = await self._current_settings()
        return bool(settings.get("suite_enabled", True))

    def _ocr_client(self, settings: dict[str, Any]):
        """OCR sidecar 客户端（懒建缓存）。本体默认 data/yolo/wiki_ontology.json。"""
        if self.state.ocr is None:
            from .ocr import SidecarOcr

            onto = str(settings.get("ocr_ontology") or "wiki_ontology.json")
            self.state.ocr = SidecarOcr(
                self.data_path("yolo") / onto,
                min_confidence=float(settings.get("ocr_min_confidence") or 0.55),
                python_exe=str(
                    settings.get("ocr_python")
                    or settings.get("yolo_python")
                    or "python"
                ),
            )
        return self.state.ocr

    def _zmd_client(self, settings: dict[str, Any] | None = None) -> ZmdLogsRunner:
        """ZMDLogs 事件流子进程管理器（懒建缓存）。

        构造不启动任何进程——只有 `zmdlogs_enabled=true` 且 `zmdlogs_autostart=true`
        时才在 startup 拉起。这里只读配置，不做 IO。
        """
        if self.state.zmd is None:
            s = settings or {}
            try:
                port = int(s.get("zmdlogs_ws_port") or DEFAULT_WS_PORT)
            except (TypeError, ValueError):
                port = DEFAULT_WS_PORT
            self.state.zmd = ZmdLogsRunner(
                repo_dir=str(s.get("zmdlogs_repo") or ""),
                python_exe=str(s.get("zmdlogs_python") or ""),
                ws_port=port,
                log_dir=str(s.get("zmdlogs_log_dir") or ""),
                rsa_key=str(s.get("zmdlogs_rsa_key") or ""),
                enabled=bool(s.get("zmdlogs_enabled", False)),
                log_level=str(s.get("zmdlogs_log_level") or "INFO"),
                exe_path=str(s.get("zmdlogs_exe") or ""),
                launch_mode=str(s.get("zmdlogs_launch_mode") or ""),
            )
        return self.state.zmd

    def _zmd_start(self, settings: dict[str, Any]) -> None:
        """按配置拉起 ZMDLogs 事件流。

        默认不拉：抓包要 Npcap + 管理员权限 + RSA 私钥，与插件其余部分（只读截图
        识别）的权限模型不同。两个闸门都过才启动；失败只记日志，如实说明原因。
        """
        runner = self._zmd_client(settings)
        if not runner.enabled:
            return
        if not bool(settings.get("zmdlogs_autostart", True)):
            self._log("info", "ZMDLogs 事件流：zmdlogs_autostart=false，按需经 ef_zmdlogs_status 手动拉起")
            return
        result = runner.start()
        if result.get("ok"):
            self._log("info", f"ZMDLogs 事件流已拉起：pid={result.get('pid')} ws={runner.ws_url()}")
            # 把子进程实际写的 logs 目录对齐给卡关辅助（WS 断连时的降级源；
            # 仅当辅助还没有 logs 目录时才对齐——显式配置的优先）。
            if self.state.assist is not None and self.state.assist.note_log_dir(
                    runner.resolved_log_dir()):
                self._log("info", "卡关辅助已对齐 ZMDLogs logs 目录（降级源就绪）")
        else:
            self._log("warning", f"ZMDLogs 事件流未启动：{result.get('error')}")

    async def _control_client(self) -> ControlClient | None:
        """M1 客户端（懒建缓存）；control_url 未配置返回 None。不在此拉起进程。"""
        if self.state.control is not None and self.state.control is not False:
            return self.state.control
        if self.state.control is False:
            return None
        settings = await self._current_settings()
        url = str(settings.get("control_url") or "").strip()
        if not url:
            self.state.control = False
            return None
        install = find_install(str(settings.get("maaend_dir") or ""))
        client = ControlClient(
            url,
            install_root=str(install.get("root") or ""),
            spawn=bool(settings.get("control_spawn", True)),
            log=lambda msg: self._log("info", str(msg)),
        )
        self.state.control = client
        return client

    # =========================================================== 基础

    def _log(self, level: str, message: str) -> None:
        """日志。message 必须已格式化：宿主 logger 风格不定，预格式化对谁都成立。"""
        logger = getattr(self, "logger", None) or getattr(
            getattr(self, "ctx", None), "logger", None
        )
        emit = getattr(logger, level, None) or getattr(logger, "info", None)
        if emit is None:
            return
        try:
            emit(message)
        except Exception:  # noqa: BLE001 - 日志永远不能影响业务
            pass

    async def _settings_snapshot(self) -> dict[str, Any]:
        """读一次完整配置（一次 IPC 往返）；后端异常时整体退回默认值。"""
        raw: dict[str, Any] = {}
        try:
            dumped = await self.config.dump()
            if isinstance(dumped, dict):
                raw = dumped
        except Exception as exc:  # noqa: BLE001
            self._log("warning", f"读取配置失败，使用默认值：{exc}")
        section = raw.get(CONFIG_PREFIX)
        if not isinstance(section, dict):
            section = {}
        self.state.settings = section
        self._sync_fail_threshold(section)
        return section

    def _sync_fail_threshold(self, settings: dict[str, Any]) -> None:
        """把 `fail_demotion_threshold` 接进 FailTracker。

        这个键从 v0.1 起就写在配置里、注释也写明「连续失败 N 次后建议降级」，
        但此前没人读它 —— FailTracker 一直用硬编码的 2。重构阶段 2 补上：
        配置热更后阈值随之生效，非法值退回 2。
        """
        raw = settings.get("fail_demotion_threshold")
        try:
            threshold = int(raw) if raw is not None else 2
        except (TypeError, ValueError):
            threshold = 2
        self.state.fails.threshold = max(1, threshold)

    async def _current_settings(self) -> dict[str, Any]:
        return self.state.settings if self.state.settings is not None else await self._settings_snapshot()

    # =========================================================== 生命周期

    async def on_startup(self, **_):
        settings = await self._settings_snapshot()
        install = find_install(str(settings.get("maaend_dir") or ""))
        windows = count_windows()
        self._log(
            "info",
            f"MaaEnd 桥接入座：found={install['found']} runtime_ready={install['runtime_ready']} "
            f"allow_run={bool(settings.get('allow_run', False))} desktop_windows={windows}",
        )
        self.report_status(
            {
                "status": "ready",
                "maaend_found": install["found"],
                "runtime_ready": install["runtime_ready"],
                "allow_run": bool(settings.get("allow_run", False)),
            }
        )
        await self._control_client()  # M1：按配置建客户端（真正 spawn 在 run 时）
        self._stream_start(settings)   # 线程 + sidecar 子进程，与事件循环无关
        self._zmd_start(settings)      # 子进程管理，同上；logs 对齐挪到 _assist_start 尾部
        # 卡关辅助与提醒循环**不在这里启动**：startup 跑在一次性的 asyncio.run() 短循环里，
        # create_task 出来的任务随循环退出即被取消（宿主 plugin/core/host.py 的
        # `_async_command_loop` 注释与 game_agent_minecraft PR #1395 先例都写明这一点）。
        # 后台任务统一由 on_command_loop_start 钩子挂到长生命周期命令循环上（v0.2.1）。
        return Ok({"status": "ready", "maaend": install["found"]})

    async def on_command_loop_start(self) -> None:
        """宿主在开始泵命令消息前调用（钩子名 `_on_command_loop_start` 由主类委托到这里）。

        钩子窗口内下行链路还没有读者，**不能走 config IPC**（config.dump 只会等超时）：
        settings 用 on_startup 填好的缓存。依赖宿主 >= 0.9.1-rc4（含
        `_async_command_loop` 对 `_on_command_loop_start` 的调用点；SDK 未导出此约定）。
        """
        self._log("info", "command-loop 钩子：卡关辅助/提醒循环已挂上")
        settings = self.state.settings or {}
        self._assist_start(settings)
        if self.state.remind_task is None:
            self.state.remind_task = asyncio.create_task(self._remind_loop())

    def _assist_start(self, settings: dict[str, Any]) -> None:
        """起卡关辅助服务（陪聊默认形态：只读订阅，不接管游戏输入）。

        失败只记日志：辅助不可用不该拖垮插件（范式文档 §七 失败隔离）。
        """
        if self.state.assist_task is not None:
            return
        try:
            # logs 目录不做启动期猜测：ZoneReader 每轮 poll 懒探测（显式配置 >
            # ZMDLogs 启动后对齐的目录 > 常见位置），日志目录晚出现也能接上，
            # 也不会把一次性的猜测固化成"显式配置"挡住后续兜底。
            cfg = AssistConfig.from_settings(settings)
            self.state.assist = AssistService(
                cfg,
                log=self._log,
                push=self._assist_push,
                scene_probe=self._assist_scene_probe_sync,
            )
            self.state.assist_task = asyncio.create_task(self.state.assist.run())
            self._log(
                "info",
                "卡关辅助已就绪："
                f"mode={cfg.assist_mode} conf={cfg.confidence_threshold} "
                f"consec={cfg.consecutive_fail_threshold} enabled={cfg.enabled}",
            )
            # 降级源对齐：_zmd_start（startup 阶段）先于钩子跑过时，这里补一次
            # note_log_dir；它只在未设置时写入，重复调用安全（v0.2.1，契约裁决 M2）。
            if self.state.zmd is not None:
                self.state.assist.note_log_dir(self.state.zmd.resolved_log_dir())
        except Exception as exc:  # noqa: BLE001 - 辅助失败不拖垮插件
            self._log("warning", f"卡关辅助启动失败（插件继续工作）：{exc}")

    def _assist_push(self, text: str) -> None:
        """把卡关建议推进聊天。`push_message` 是同步转发（DESIGN.md：不要 await）。"""
        push = getattr(self.main_plugin, "push_message", None)
        if push is None:
            return
        result = push(source="maa_end_assist", visibility=["chat"], ai_behavior="read",
                      parts=[{"type": "text", "text": text}])
        if inspect.isawaitable(result):  # 版本差异兜底：awaitable 就交给事件循环
            return asyncio.ensure_future(result)
        if isinstance(result, dict) and not result.get("submitted", True):
            raise RuntimeError(f"push_message 未提交：{result.get('reason')}")  # _emit 记 errors
        return None

    def _stream_start(self, settings: dict[str, Any]) -> None:
        """按配置起数据流服务(已起则跳过;失败不拖垮插件,如实记日志)。"""
        if self.state.stream is not None:
            return
        if not bool(settings.get("stream_enabled", True)):
            return
        try:
            from .infra.probe import find_game_window
            from .stream import StreamService

            class_regex = str(settings.get("window_class_regex") or "")
            title_regex = str(settings.get("window_title_regex") or "")

            def probe() -> dict[str, Any]:
                w = find_game_window(class_regex, title_regex)
                return {
                    "found": bool(w.get("found")),
                    "hwnd": int(w.get("hwnd") or 0),
                    "title": str(w.get("title") or ""),
                    "class": str(w.get("class") or ""),
                }

            model_name = str(settings.get("yolo_model") or "endfield_yolo_v2.pt")
            # sidecar 模式:宿主冻结进程 matplotlib 被 Nuitka 排除,ultralytics
            # 只能在系统 Python 子进程跑(接口见 yolo.SidecarDetector)
            from .yolo import SidecarDetector
            detector = SidecarDetector(
                self.data_path("yolo") / model_name,
                confidence=float(settings.get("yolo_confidence") or 0.4),
                python_exe=str(settings.get("yolo_python") or "python"),
            )
            # 干员专精模型（单类 operator）——可选第二路。
            # 与主模型并行跑、结果合并：主模型 9 类保全局感知，专精模型补干员精度
            # （实测 P 0.953 / R 0.908，见 docs/TRAINING-PIPELINE.md）。
            # 配置为空即关闭，不占额外显存与子进程。
            detector_operator = None
            op_model = str(settings.get("yolo_operator_model") or "").strip()
            if op_model:
                try:
                    detector_operator = SidecarDetector(
                        self.data_path("yolo") / op_model,
                        confidence=float(settings.get("yolo_operator_confidence") or 0.3),
                        python_exe=str(settings.get("yolo_python") or "python"),
                    )
                    self._log("info", f"干员专精模型已装载: {op_model}")
                except Exception as exc:  # noqa: BLE001 - 专精模型失败不拖垮主流
                    self._log("warning", f"干员专精模型初始化失败(忽略): {str(exc)[:100]}")
            self.state.stream = StreamService(
                log_path=self.data_path("maa-pi-runner") / "debug" / "maafw.log",
                window_probe=probe,
                detector=detector,
                detector_operator=detector_operator,
                settings=settings,
                control=(
                    self.state.control
                    if isinstance(self.state.control, ControlClient)
                    else None
                ),
            )
            self.state.detector = detector
            self.state.detector_operator = detector_operator
            self.state.stream.start()
            self._log("info", "maa_end 数据流已启动(tasks/window/yolo)")
        except Exception as exc:  # noqa: BLE001 - 流失败不拖垮插件
            self._log("warning", "数据流启动失败(插件继续工作): " + str(exc)[:120])

    async def on_config_change(self, old_config=None, new_config=None, mode=None, **_):
        self.state.settings = None
        if isinstance(self.state.control, ControlClient):
            self.state.control.stop_spawned()  # control_url 可能变了：收旧实例，惰性重建
        self.state.control = None
        self._restart_assist()
        self._zmd_reload()
        return Ok({"status": "config_reloaded"})

    def _zmd_reload(self) -> None:
        """配置热更新：事件流的仓库路径/端口/闸门可能变了 → 收旧进程并按新配置重建。

        与 `_restart_assist` 同思路，但**不自动重启**：抓包进程在用户没要求时就重拉，
        等于擅自占用网卡与管理员权限。所以这里只清干净，等下次显式调用或重启插件。
        """
        if self.state.zmd is not None:
            self.state.zmd.stop()
        self.state.zmd = None

        async def _later() -> None:
            try:
                settings = await self._current_settings()
                self._zmd_start(settings)
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"ZMDLogs 事件流重建失败：{exc}")

        try:
            asyncio.get_running_loop().create_task(_later())
        except RuntimeError:
            pass  # 无事件循环（单测直接调）：跳过重建

    def _restart_assist(self) -> None:
        """配置热更新：三项参数可能变了 → 重建辅助服务（旧任务先停）。

        这是「设置面板改参数即时生效」的实现点——不重启插件。
        """
        if self.state.assist is not None:
            self.state.assist.stop()
        if self.state.assist_task is not None:
            self.state.assist_task.cancel()
            self.state.assist_task = None
        self.state.assist = None

        async def _later() -> None:
            try:
                settings = await self._current_settings()
                self._assist_start(settings)
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"卡关辅助重建失败：{exc}")

        try:
            asyncio.get_running_loop().create_task(_later())
        except RuntimeError:
            pass  # 无事件循环（单测直接调）：跳过重建

    async def on_shutdown(self, **_):
        if self.state.m6 is not None:  # M6 键鼠接管：最先停（抬起全部按键，防卡键）
            try:
                await self.state.m6.stop("插件卸载")
            except Exception:  # noqa: BLE001 - shutdown 里尽力而为
                pass
            self.state.m6 = None
        if self.state.remind_task is not None:
            self.state.remind_task.cancel()
            self.state.remind_task = None
        if self.state.assist is not None:  # 卡关辅助：先置停止位再 cancel
            self.state.assist.stop()
            self.state.assist = None
        if self.state.assist_task is not None:
            self.state.assist_task.cancel()
            self.state.assist_task = None
        if self.state.stream is not None:
            self.state.stream.stop()
            self.state.stream = None
        if self.state.ocr is not None:  # 收掉 OCR sidecar 子进程
            self.state.ocr.stop()
            self.state.ocr = None
        if self.state.zmd is not None:  # 收掉 ZMDLogs 事件流子进程
            self.state.zmd.stop()
            self.state.zmd = None
        if self.state.m3_active:  # M3 会话中途关插件：尽力还原全局套组模式
            try:
                settings = await self._current_settings()
                await exit_nitrogen(self._suite_client(settings))
            except Exception:  # noqa: BLE001 - shutdown 里尽力而为
                pass
            self.state.m3_active = False
        if isinstance(self.state.control, ControlClient):
            self.state.control.stop_spawned()
        if self.state.runner is not None and self.state.runner.running():
            self.state.runner.stop()
        return Ok({"status": "stopped"})

    # 入口方法已按域拆到 maaend/api/：
    #   install / task / assist / vision / knowledge / ops / m6 / llm_tools
    # 本类只保留共享状态（AppState）、基础设施客户端、生命周期、提醒与流服务启停。

