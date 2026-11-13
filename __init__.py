"""终末地助手 —— N.E.K.O. 原生插件（自 game_suite 聚合拆出，独立成插件）。

自动化全部由 MaaEnd / MaaFramework 本体执行；本插件的识别层（场景判别 / YOLO /
OCR / 事件流）全程只读，不触碰游戏进程、不模拟输入。执行类入口保留 allow_run 闸门
（默认关闭），打开后才会接管游戏窗口。
"""

from __future__ import annotations

from plugin.sdk.plugin import NekoPluginBase, Ok, lifecycle, neko_plugin

try:  # 真实 SDK 不在包层导出收集器，退回子模块；桩 SDK 则相反
    from plugin.sdk.plugin import collect_llm_tool_methods
except ImportError:  # pragma: no cover
    from plugin.sdk.plugin.llm_tool import collect_llm_tool_methods

try:  # Hosted UI 装饰器（老版本 SDK 没有 → 面板降级为不可用，不影响插件本体）
    from plugin.sdk.plugin import ui
except ImportError:  # pragma: no cover
    ui = None

from .maaend.router import MaaEndRouter


def _ui_context(context_id: str):
    """安全包装 `@ui.context`：SDK 没有 ui 时退化为无操作装饰器。"""

    def decorate(fn):
        if ui is None:
            return fn
        try:
            return ui.context(id=context_id)(fn)
        except Exception:  # noqa: BLE001 - 面板失败不能拖垮插件
            return fn

    return decorate


@neko_plugin
class MaaEndPlugin(NekoPluginBase):
    """终末地助手。"""

    # 声明 router 类，供主进程静态扫描 entry 元数据
    __routers__ = [MaaEndRouter]

    def __init__(self, ctx):
        super().__init__(ctx)
        self.maa_router = MaaEndRouter()
        # 注册 router —— 必须在 __init__ 中，collect_entries 在 startup 之前调用
        self.include_router(self.maa_router)
        # 宿主静态扫描不收 router 入口 → 按全局 30s 掐 ef_run_task（声明 120s），重扫后还丢入口。
        # shortcut: 借 SDK 私有的 _notify_dynamic_entry_registered 走 ENTRY_UPDATE 补登记，
        # 宿主 registry.scan_static_metadata 扫 __routers__ 的版本发布后删掉。
        notify = getattr(self, "_notify_dynamic_entry_registered", None)
        if callable(notify):
            for entry_id, handler in self.maa_router.collect_entries().items():
                try:
                    notify(entry_id, handler.meta, enabled=True)
                except Exception:  # noqa: BLE001 - 补登记失败只是退回旧行为
                    pass
        # 基类的 @llm_tool 自动注册只扫描插件类自身；工具都在 router 上，公开路径补注册
        for meta, bound in collect_llm_tool_methods(self.maa_router):
            self.register_llm_tool(
                name=meta.name,
                description=meta.description,
                parameters=meta.parameters,
                handler=bound,
                timeout=meta.timeout_seconds,
                role=meta.role,
            )

    # Hosted UI：面板的 props.state（id 必须匹配 plugin.toml 的 context = "maa_end_assist"）
    @_ui_context("maa_end_assist")
    async def maa_end_assist(self, **_):
        """面板状态：三项参数 + 运行状态 + 最近建议。

        面板只读这里；改参数走 `ef_assist` 动作（action=set）。
        `**_` 是必需的：宿主会注入 `_ctx`（context 渲染时的调用上下文）。
        """
        try:
            return await self.maa_router.assist_context()
        except Exception as exc:  # noqa: BLE001 - 面板拿不到状态也要能渲染
            return {"running": False, "reason": f"读取状态失败：{exc}"}

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        return await self.maa_router.on_startup(**_)

    @lifecycle(id="config_change")
    async def on_config_change(self, old_config=None, new_config=None, mode=None, **_):
        return await self.maa_router.on_config_change(
            old_config=old_config, new_config=new_config, mode=mode)

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        handler = getattr(self.maa_router, "on_shutdown", None)
        if handler is None:
            return Ok({"status": "stopped"})
        return await handler(**_)

    # 宿主私有约定（非 SDK 导出）：`_async_command_loop` 开始泵消息前会在插件实例上
    # getattr 这个名字并 await。后台任务必须经这里挂到长生命周期命令循环；
    # 依赖宿主 >= 0.9.1-rc4。详见 router.on_command_loop_start。
    async def _on_command_loop_start(self) -> None:
        try:
            await self.maa_router.on_command_loop_start()
        except Exception as exc:  # noqa: BLE001 - 钩子失败不阻断命令循环（宿主侧亦有兜底）
            self.maa_router._log(
                "warning", f"command-loop 钩子失败（后台循环未挂上）：{type(exc).__name__}: {exc}"
            )
