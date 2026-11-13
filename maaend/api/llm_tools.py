"""llm_tool 薄包装：统一错误包装后转调对应 entry。

由 `maaend/router.py` 拆分而来（2026-11-04，见 docs/REFACTOR-PLAN.md 阶段 3）。
状态一律经 `self.state`（AppState）读写，不直接持有实例字段。
"""

from __future__ import annotations

import asyncio

from plugin.sdk.plugin import (
    llm_tool,
)

from ._helpers import unwrap_tool_result


class LLMToolsAPI:
    """llm_tool 薄包装：统一错误包装后转调对应 entry。"""

    @llm_tool(
        name="maa_remind",
        description="设置终末地相关提醒并到点推送(M4 人工辅助)。"
        "常用:「任务结束叫我」kind=after_task,value 留空;"
        "「30分钟后提醒我」kind=in_minutes,value=30;"
        "「每天 12:00 提醒」kind=at_time,value=12:00;"
        "「理智满了/画面出现某文字叫我」kind=ocr_text,value=要等待的文本"
        "(游戏前台时每 30s 读屏一次,不抢焦点)。"
        "查/取消用面板 ef_reminders。会话内存态,插件重载后清空。",
        parameters={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "after_task|at_time|in_minutes"},
                "value": {"type": "string", "description": "任务entry(空=任意)|HH:MM|分钟数"},
                "message": {"type": "string", "description": "提醒附言"},
            },
            "required": ["kind"],
        },
        timeout=15.0,
    )
    async def maa_remind_tool(self, kind: str = "in_minutes", value: str = "30",
                              message: str = "", **_):
        result = await self.remind(kind=str(kind or "in_minutes"),
                                   value=str(value or ""), message=str(message or ""))
        return unwrap_tool_result(result)

    @llm_tool(
        name="maa_ocr",
        description="读终末地游戏画面上的文字(OCR)。用户问「现在理智多少」「屏幕上说什么」"
        "「日常还剩什么」时用它。会把游戏窗口带到前台(截图需要)。"
        "roi 可选 [x1,y1,x2,y2] 0-1 比例数组缩小范围。"
        "设条件提醒用 maa_remind(kind=ocr_text,value=要等待的文本)。",
        parameters={
            "type": "object",
            "properties": {
                "roi": {"type": "string", "description": "[x1,y1,x2,y2] 0-1 比例 JSON,空=全屏"},
            },
            "required": [],
        },
        timeout=60.0,
    )
    async def maa_ocr_tool(self, roi: str = "", **_):
        result = await self.ocr(roi=str(roi or ""))
        return unwrap_tool_result(result)

    @llm_tool(
        name="maa_adb",
        description="查 ADB 设备列表或对手机端/模拟器截图(action=list|screenshot,serial 设备地址)。"
        "用户要跑终末地手机端任务时:先 list 拿 serial,再 maa_run_task 带 controller=ADB+adb_serial。"
        "截图存 data/adb/ 返回路径。adb 路径由 maa.adb_path 配置。",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "list|screenshot"},
                "serial": {"type": "string", "description": "screenshot 时的设备地址"},
            },
            "required": [],
        },
        timeout=30.0,
    )
    async def maa_adb_tool(self, action: str = "list", serial: str = "", **_):
        result = await self.adb_tools(action=str(action or "list"), serial=str(serial or ""))
        return unwrap_tool_result(result)

    @llm_tool(
        name="maa_status",
        description="查看 MaaEnd / 终末地自动化的当前状态：安装、运行库版本、游戏窗口、任务清单规模、"
        "任务闸门。用户问「终末地挂机什么情况」时先调用它。",
        parameters={"type": "object", "properties": {}, "required": []},
        timeout=60.0,
    )
    async def maa_status_tool(self, **_):
        result = await self.status()
        return unwrap_tool_result(result)

    @llm_tool(
        name="maa_decide",
        description="把『帮我做终末地日常/基质/跑商…』这类请求交给游戏决策层(game_decision)"
        "+任务清单匹配，返回排序候选任务与理由；run=true 时执行第一候选"
        "（需 maa.allow_run 放行，NitroGen 在玩时拒绝）。用户没点名具体任务时先用它，别瞎猜任务名。",
        parameters={
            "type": "object",
            "properties": {
                "request": {"type": "string", "description": "用户请求原文，如『清一下终末地日常』"},
                "run": {"type": "boolean", "description": "true=执行第一候选（默认 false 只出建议）"},
            },
            "required": ["request"],
        },
        timeout=120.0,
    )
    async def maa_decide_tool(self, request: str = "", run: bool = False, **_):
        result = await self.decide_task(request=request, run=bool(run))
        return unwrap_tool_result(result)

    @llm_tool(
        name="maa_agent",
        description="M3 视觉代理接管(NitroGen):切换套组模式并接管手柄玩终末地。"
        "**危险操作**:必须用户明确同意后传 confirm=true;通用但**无完成保证**,进度靠画面判断。"
        "停止用 maa_stop_task(会停代理并把套组模式切回 decision)。"
        "仅在 MaaEnd 不可用或用户点名时提议,常规任务优先 maa_run_task。",
        parameters={
            "type": "object",
            "properties": {
                "confirm": {"type": "boolean", "description": "用户明确同意接管手柄才能 true"},
                "target_process": {"type": "string", "description": "目标进程名,默认 Endfield.exe"},
                "force": {"type": "boolean", "description": "有同伴在玩也强行启动(默认否)"},
            },
            "required": [],
        },
        timeout=60.0,
    )
    async def maa_agent_tool(self, confirm: bool = False, target_process: str = "",
                             force: bool = False, **_):
        result = await self.start_agent(
            confirm=bool(confirm), target_process=str(target_process or ""),
            force=bool(force))
        return unwrap_tool_result(result)

    @llm_tool(name="maa_run_task", description="经 MaaEnd 官方 MaaPiCli 跑一个任务(完整 PI 语义:选项 case、agent 子进程)。参数 name 任务名(先 list_tasks 查);options_json 选项选择映射 JSON 对象,默认空=用 default_case;force=true 覆盖套组互斥(NitroGen 在玩时默认拒绝);controller 显式控制器名(默认自动:Windows 优先 Win32;手机端用 ADB/CloudADB);adb_serial ADB 设备地址(controller=ADB 时必填,先用 maa_adb 查)。**会接管游戏窗口/设备输入**(闸门 maa.allow_run=true 时放行)。",
              parameters={"type": "object", "properties": {"name": {"type": "string", "description": "任务名(先 maa_list_tasks 查)"}, "options_json": {"type": "string", "description": "选项选择映射 JSON 对象字符串,空=default_case"}, "force": {"type": "boolean", "description": "覆盖套组互斥"}, "controller": {"type": "string", "description": "控制器名,空=自动"}, "adb_serial": {"type": "string", "description": "ADB 设备地址(controller=ADB 时必填)"}}, "required": ["name"]},
              timeout=120.0)
    async def maa_run_task_tool(self, *args, **kwargs):
        # 兼容各种 host 调用姿势
        name = kwargs.get("name") or (args[0] if args else "") or ""
        options_json = kwargs.get("options_json") or (args[1] if len(args) > 1 else "") or ""
        force = bool(kwargs.get("force") or (args[2] if len(args) > 2 else False))
        controller = str(kwargs.get("controller") or "")
        adb_serial = str(kwargs.get("adb_serial") or "")
        result = await self.run_task(
            name=name, options_json=options_json, force=force,
            controller=controller, adb_serial=adb_serial)
        return unwrap_tool_result(result)

    @llm_tool(name="maa_list_tasks", description="列出本机 MaaEnd interface.json 声明的任务(只读:清单 + 选项定义,不会执行)。")
    async def maa_list_tasks_tool(self, **_):
        result = await self.list_tasks()
        return unwrap_tool_result(result)

    @llm_tool(name="maa_stop_task", description="终止 MaaPiCli 运行器进程(任务立即停止;游戏本身不受影响)。**需要先 maa_run_task 启动过任务**。")
    async def maa_stop_task_tool(self, **_):
        result = await self.stop_task()
        return unwrap_tool_result(result)

    @llm_tool(name="maa_task_state", description="查询 MaaPiCli 运行器状态:运行中 / 已退出(退出码) + 输出日志尾部。")
    async def maa_task_state_tool(self, **_):
        result = await self.task_state()
        return unwrap_tool_result(result)

    @llm_tool(name="maa_stream_read", description="读取 maa_end 数据流(任务事件/窗口状态/YOLO 检测)。参数 category 可选 tasks/window/yolo(默认全部分类);since 上次读到的最大 id(默认 0=从最早缓冲开始);limit 最多返回条数(默认 50)。返回 entries 数组 + latest_id(下次调用传 since=latest_id 即增量拉取)。",
              parameters={"type": "object", "properties": {"category": {"type": "string", "enum": ["tasks", "window", "yolo"]}, "since": {"type": "integer", "description": "上次读到的最大 id,默认 0"}, "limit": {"type": "integer", "description": "最多返回条数,默认 50"}}})
    async def maa_stream_read_tool(self, **kwargs):
        if self.state.stream is None:
            return {"output": {"error": "数据流未启动(检查 maa.stream_enabled)"}, "is_error": True}
        category = str(kwargs.get("category") or "")
        try:
            since = int(kwargs.get("since") or 0)
        except (TypeError, ValueError):
            since = 0
        try:
            limit = max(1, min(int(kwargs.get("limit") or 50), 100))
        except (TypeError, ValueError):
            limit = 50
        entries = self.state.stream.store.read(category=category, since=since, limit=limit)
        return {
            "entries": entries,
            "latest_id": self.state.stream.store.latest_id(),
            "count": len(entries),
        }

    @llm_tool(name="maa_yolo_detect", description="立即对游戏窗口截图跑一次 YOLO 目标检测并返回结果(同时写入 yolo 流)。无需参数;返回 size/count/objects[{label,confidence,box}]。默认用随插件附带的终末地 9 类模型(endfield_yolo_v2.pt:enemy/enemy_alert/boss/operator/enemy_name/operator_name/item_name/ui_button/ui_icon,见 data/yolo/endfield_yolo_v2.meta.json);可在 maa.yolo_model 换自训模型。")
    async def maa_yolo_detect_tool(self, **kwargs):
        if self.state.stream is None:
            return {"output": {"error": "数据流未启动"}, "is_error": True}
        import traceback as _tb
        try:
            result = await asyncio.to_thread(self.state.stream.detect_now)
        except Exception as exc:  # noqa: BLE001 - 原始错误透出(numpy 版本冲突等可定位)
            return {
                "output": {
                    "error": type(exc).__name__ + ": " + str(exc)[:200],
                    "traceback_tail": _tb.format_exc().strip().splitlines()[-3:],
                },
                "is_error": True,
            }
        if isinstance(result, dict) and result.get("error"):
            return {"output": result, "is_error": True}
        return result

    @llm_tool(name="maa_pyenv_probe", description="诊断:本插件子进程的 Python 环境探针(版本/关键库可导入性/sys.path)。排查 YOLO/依赖问题时先跑这个。无参数。")
    async def maa_pyenv_probe_tool(self, **kwargs):
        import importlib.util as _u
        import sys as _sys
        libs = {}
        for name in ("torch", "numpy", "PIL", "ultralytics", "cv2", "yaml", "matplotlib", "pandas", "tqdm", "psutil", "py_cpuinfo"):
            # Nuitka 冻结进程对 --nofollow-import-to 排除模块(如 matplotlib)
            # 在 find_spec 阶段就抛 RuntimeError —— 必须逐库 try/except,
            # 把每个库的真实状态(ok / excluded:<msg> / missing)如实记下
            try:
                libs[name] = "ok" if _u.find_spec(name) else "missing"
            except Exception as exc:  # noqa: BLE001
                libs[name] = "excluded: " + str(exc)[:60]
        return {
            "python": _sys.version.split()[0],
            "executable": _sys.executable,
            "libs": libs,
            "sys_path": _sys.path,
        }
