"""入口层（API）—— `@plugin_entry` / `@llm_tool` / `@ui.action` 的宿主。

2026-11-04 阶段 3 已完成拆分，`router.py` 从 2878 行降到 ~445 行：

    api/_helpers.py    公共纯函数（取窗口 / 工具错误包装）
    api/install.py     安装、状态、任务清单、ADB、打开 App
    api/task.py        任务执行、模式阶梯、排轴、回放、视觉代理
    api/assist.py      陪玩辅助与面板
    api/vision.py      YOLO / OCR / 场景 / 数据流
    api/knowledge.py   知识库、决策派单、套组
    api/ops.py         提醒、报告、ZMDLogs、每日编排
    api/m6.py          GameScaling 代理
    api/llm_tools.py   22 个 llm_tool 薄包装

**拆分方式**：SDK 的 `PluginRouter` 用 `inspect.getmembers_static(type(self))`
静态扫描类成员（会遍历 MRO），所以 `MaaEndRouter` 用多继承组合这些 mixin
即可 —— 不需要多个 router 实例，`self` 状态天然共享。

**约束**：mixin 之间不得互相 import；共享状态一律经 `state.AppState`。
`router.py` 只保留：AppState、基础设施客户端、生命周期、提醒/流服务启停。
"""

__all__: list[str] = []
