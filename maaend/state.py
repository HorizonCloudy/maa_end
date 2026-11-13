"""插件运行时状态 —— 把 router 上 21 个散字段收成一个可注入对象。

为什么要有这个文件（docs/REFACTOR-PLAN.md §2 #1 / §3.1）：

`MaaEndRouter` 原本在 `__init__` 里挂了 21 个 `self._*` 字段，被 86 个方法
共享读写。拆 mixin 时，如果状态还散在 `self` 上，每个 mixin 都会隐式依赖
「别的 mixin 什么时候给这个字段赋过值」—— 那只是把 2878 行的大泥球切成
七个互相看不见的小泥球。

`AppState` 把「有哪些可变状态」变成一份可读的清单，mixin 只依赖它，
不依赖彼此。

字段语义（不要按名字猜，这里有踩过的坑）：

- `control`：**三态**。`None` = 还没检查过；`False` = 检查过、没配置；
  实例 = 可用客户端。用 `False` 而不是 `None` 当哨兵是为了不重复做
  「配置里有没有 control_url」这个判断。
- `fails`：连续失败计数，阈值来自 `maa.fail_demotion_threshold`
  （阶段 2 之前这个配置键没人读，一直用硬编码 2）。
- `mode` / `m3_active` / `m6`：模式阶梯（M0–M6）的当前态，被
  `status` / `task_state` / `stop_task` / `modes` / `suite_probe` 五处共享。
- `detector`：YOLO sidecar 客户端，由 `_stream_start` 填充；`ef_ocr` 与
  OCR 条件提醒复用它（截图+OCR 在同一进程里做，省一次启动）。
- `settings`：`config.dump()` 的 `[maa]` 段缓存；`on_config_change` 置 None
  让它下次重新拉取。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .executor import FailTracker
from .remind import ReminderStore


@dataclass
class AppState:
    """一次插件会话内的全部可变状态。不跨进程、不持久化。"""

    # ---- 配置 ----
    settings: dict[str, Any] | None = None

    # ---- MaaFramework / 安装 ----
    fw: Any = None
    fw_bin: str = ""
    runner: Any = None          # PiRunner，最近一次启动的运行器

    # ---- 识别层 ----
    stream: Any = None          # StreamService
    detector: Any = None        # SidecarDetector（YOLO sidecar 客户端）
    detector_operator: Any = None  # 干员专精模型（单类 operator）的第二路 sidecar；None = 未配置
    ocr: Any = None             # SidecarOcr（懒建）
    ocr_warned: bool = False

    # ---- 套组 / 控制服务 ----
    suite: Any = None
    control: Any = None         # None=未检查 / False=未配置 / 实例=可用

    # ---- 模式阶梯 ----
    mode: str = ""              # m1/m2/m3；空 = 尚未执行
    m3_active: bool = False
    fails: FailTracker = field(default_factory=FailTracker)
    m6: Any = None              # GamescalingAgent

    # ---- M4 提醒 ----
    reminders: ReminderStore = field(default_factory=ReminderStore)
    remind_cursor: int = 0
    remind_task: Any = None

    # ---- 陪玩辅助 ----
    assist: Any = None          # AssistService
    assist_task: Any = None

    # ---- 外部子进程 ----
    zmd: Any = None             # ZmdLogsRunner
    daily: Any = None           # maa_daily action=run 的后台编排器
