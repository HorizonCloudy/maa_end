#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""maa_end 插件的端到端验证（桩 SDK + 真实 Win32 + 可选真实 MaaFramework）。

分层验证，越往下越真：

1. 桩 SDK + 假安装树：探测、清单解析（用 MaaEnd 仓库的真实 interface.json 片段做
   夹具）、闸门拒绝——不依赖任何外部东西；
2. 真实 Win32 枚举：桌面窗口数 > 0，且正则匹配路径真实执行；
3. 真实 MaaFramework（如果测试机上有运行库，路径由环境变量 MAA_FW_BIN 指定）：
   加载 DLL → 版本号 → 资源加载 → 任务器初始化。没有就跳过并如实说明。

跑法：python tests/stub_run.py    报告写到 tests/stub-report.txt
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
import traceback
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
# suite 的 #3483 HTTP 回退不许打到本机真实宿主：端口 0 立即连接失败 → 走桩 SDK 路径
os.environ["NEKO_PLUGIN_HOST_URL"] = "http://127.0.0.1:0"
PLUGIN_DIR = HERE.parent
REPORT = HERE / "stub-report.txt"

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), label, detail))
    line = "[%s] %s" % ("PASS" if ok else "FAIL", label)
    if detail:
        line += "  -> " + detail
    print(line, flush=True)


# --------------------------------------------------------------------------- #
# 桩 SDK
# --------------------------------------------------------------------------- #

STUB_SDK = '''\
"""桩 plugin.sdk.plugin。"""
from __future__ import annotations

import inspect
from pathlib import Path


class SdkError(Exception):
    def __init__(self, message="", *args):
        super().__init__(message, *args)
        self.message = message

    def __str__(self):
        return str(self.message)


class Ok:
    def __init__(self, value=None):
        self.value = value


class Err:
    def __init__(self, error=None):
        self.error = error


class _Meta:
    def __init__(self, kind, **kw):
        self.kind = kind
        self.kw = kw


def _marker(kind):
    def factory(**kw):
        def deco(func):
            func.__neko_meta__ = _Meta(kind, **kw)
            return func
        return deco
    return factory


def neko_plugin(cls=None, **kw):
    if cls is not None:
        cls.__neko_meta__ = _Meta("plugin", **kw)
        return cls

    def deco(target):
        target.__neko_meta__ = _Meta("plugin", **kw)
        return target
    return deco


LLM_TOOL_META_ATTR = "__neko_llm_tool_meta__"


class LlmToolMeta:
    def __init__(self, name, description="", parameters=None, timeout_seconds=30.0, role=None):
        self.name = name
        self.description = description
        self.parameters = parameters or {}
        self.timeout_seconds = timeout_seconds
        self.role = role


def llm_tool(**kw):
    def deco(func):
        meta = LlmToolMeta(
            name=kw.get("name") or func.__name__,
            description=kw.get("description", ""),
            parameters=kw.get("parameters") or {},
            timeout_seconds=float(kw.get("timeout", 30.0)),
            role=kw.get("role"),
        )
        setattr(func, LLM_TOOL_META_ATTR, meta)
        func.__neko_meta__ = _Meta("llm_tool", **kw)
        return func
    return deco


def collect_llm_tool_methods(instance):
    out = []
    for _, member in inspect.getmembers(type(instance)):
        if not callable(member):
            continue
        meta = getattr(member, LLM_TOOL_META_ATTR, None)
        if not isinstance(meta, LlmToolMeta):
            continue
        bound = getattr(instance, member.__name__, None)
        if callable(bound):
            out.append((meta, bound))
    return out


plugin_entry = _marker("entry")
lifecycle = _marker("lifecycle")


class _StubConfig:
    def __init__(self, data):
        self._data = data

    async def dump(self, **_):
        return dict(self._data)

    async def update(self, patch, **_):
        """按宿主语义合并**段名做键**的嵌套字典，返回持久化后的完整配置。"""
        for section, values in (patch or {}).items():
            current = self._data.get(section)
            if isinstance(current, dict) and isinstance(values, dict):
                current.update(values)
            else:
                self._data[section] = values
        return dict(self._data)


class PluginRouter:
    """桩 router：绑定主插件后，把自身没有的访问器转发过去。"""

    def __init__(self, *a, **k):
        self.name = k.get("name") or type(self).__name__
        self.main_plugin = None

    def __getattr__(self, item):
        main = object.__getattribute__(self, "main_plugin")
        if main is None:
            raise AttributeError(item)
        return getattr(main, item)


class NekoPluginBase:
    def __init__(self, ctx):
        self.ctx = ctx
        self.plugin_id = getattr(ctx, "plugin_id", "")
        self.plugin_dir = getattr(ctx, "plugin_dir", None)
        self.storage_dir = getattr(ctx, "storage_dir", None)
        self.logger = getattr(ctx, "logger", None)
        self.config = getattr(ctx, "config", None)
        self.plugins = getattr(ctx, "plugins", None)
        self.routers = []
        self.reported = []
        self.llm_tools = self._collect_llm_tools()

    def include_router(self, router):
        router.main_plugin = self
        self.routers.append(router)
        return router

    def register_llm_tool(self, *, name, description, parameters, handler, timeout=30.0, role=None):
        if name in self.llm_tools:
            raise AssertionError(f"duplicate LLM tool name: {name}")
        self.llm_tools[name] = getattr(handler, "__qualname__", str(handler))
        return True

    def _collect_llm_tools(self):
        found = {}
        for name in dir(type(self)):
            meta = getattr(getattr(type(self), name, None), "__neko_meta__", None)
            if meta is not None and meta.kind == "llm_tool":
                found[meta.kw.get("name") or name] = name
        return found

    def report_status(self, status):
        self.reported.append(status)

    def data_path(self, *parts):
        base = Path(self.storage_dir) / "data"
        base.mkdir(parents=True, exist_ok=True)
        return base.joinpath(*parts)

    def cache_path(self, *parts):
        base = Path(self.storage_dir) / "cache"
        base.mkdir(parents=True, exist_ok=True)
        return base.joinpath(*parts)


class _UiDecorator:
    """Hosted UI 装饰器桩：只记录 context id，行为等价于真实 SDK 的标记。"""

    def __init__(self, kind):
        self.kind = kind

    def __call__(self, **kw):
        def decorate(fn):
            fn.__neko_ui__ = (self.kind, kw)
            return fn

        return decorate

    def action(self, **kw):
        return self.__call__(**kw)


ui = _UiDecorator("ui")
'''


def build_shim(workdir: Path) -> Path:
    shim = workdir / "shim"
    pkg = shim / "plugin"
    (pkg / "sdk" / "plugin").mkdir(parents=True, exist_ok=True)
    (pkg / "plugins").mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "sdk" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "sdk" / "plugin" / "__init__.py").write_text(STUB_SDK, encoding="utf-8")
    (pkg / "sdk" / "plugin" / "llm_tool.py").write_text(
        "from plugin.sdk.plugin import (\n"
        "    LLM_TOOL_META_ATTR,\n"
        "    LlmToolMeta,\n"
        "    collect_llm_tool_methods,\n"
        ")\n",
        encoding="utf-8",
    )
    (pkg / "plugins" / "__init__.py").write_text("", encoding="utf-8")
    shutil.copytree(
        PLUGIN_DIR,
        pkg / "plugins" / "maa_end",
        ignore=shutil.ignore_patterns(
            "config", "data", "cache", "__pycache__", "*.pyc", "tests",
            ".pytest_cache", ".ruff_cache",
            # 以下两类曾让每次运行都留下数 GB 垃圾（2026-11-04 实测）：
            #   .venv        —— 3 万文件的虚拟环境，测试用不到（系统 python 跑 sidecar）
            #   maa-end-stub-* / report-* / pytest-of-* —— 自引用：上次的拷贝会被再拷一遍
            ".venv", "venv",
            "maa-end-stub-*", "report-*", "pytest-of-*",
            "dist", "build",
            # tools/ 是数 GB 的 pyenv 运行库且有读限制，桩测试不用（2026-11-11 实测）
            "tools", "third_party", "archive", "tmp*",
        ),
    )
    return shim


class StubCtx:
    def __init__(self, plugin_dir: Path, storage_dir: Path, config):
        self.plugin_id = "maa_end"
        self.plugin_dir = plugin_dir
        self.storage_dir = storage_dir
        self.metadata = {}
        self.logger = None
        self.config = config


async def invoke(plugin, method: str, **kwargs):
    """宿主式调用：多塞一个 _ctx（0.9.0.1 实测行为）。"""
    return await getattr(plugin, method)(_ctx=plugin.ctx, **kwargs)


# --------------------------------------------------------------------------- #
# 夹具：MaaEnd 仓库的真实 interface.json 片段（JSON5 语法 + 空任务表 + 版本化结构）
# --------------------------------------------------------------------------- #

INTERFACE_JSON5 = """\
{
  "interface_version": 2,
  "name": "$interface.name", // 带注释
  "description": "测试清单",
  "version": "v9.9-test",
  "controller": [
    {
      "name": "Win32-Front",
      "type": "Win32",
      "win32": {
        "class_regex": "UnityWndClass",
        "window_regex": "Endfield",
        "screencap": "ScreenDC",
        "mouse": "Seize",
      }, // 尾逗号
    },
  ],
  "resource": [{ "name": "官服", "path": "{PROJECT_DIR}/resource/base" }],
  "agent": [{ "child_exec": "agent/go-service" }],
  "group": [],
  "task": [],
  "import": ["tasks_extra.json"],
}
"""


def build_fake_install(workdir: Path) -> Path:
    """造假安装树：app exe + maafw 假 DLL + 真实语法的 interface.json + 资源目录。"""
    root = workdir / "MaaEnd"
    (root / "maafw").mkdir(parents=True, exist_ok=True)
    (root / "MaaEnd.exe").write_bytes(b"MZ fake")
    for exe_dll in ("MaaFramework.dll", "MaaToolkit.dll", "MaaPiCli.exe"):
        # 假文件：探测层只看存在性；真实加载走 MAA_FW_BIN（见下）。
        (root / "maafw" / exe_dll).write_bytes(b"MZ fake")
    (root / "interface.json").write_text(INTERFACE_JSON5, encoding="utf-8")
    (root / "tasks_extra.json").write_text(
        json.dumps({"task": [{"name": "冒烟任务", "entry": "MaaEndSmoke",
                              "description": "来自 import 子清单的任务"}],
                    # 仿 RealTimeTask.json 的 AutoFightAxis → AutoFightAxisData（input 型子选项）
                    "option": {
                        "某选项": {"cases": [{"name": "某case", "option": ["子输入"]}]},
                        "子输入": {"type": "input",
                                 "inputs": [{"name": "Code", "default": ""}],
                                 "pipeline_override": {"X": {"code": "{Code}"}}},
                        "热键": {"type": "hotkey", "hotkeys": [{"name": "K"}]},
                    }}),
        encoding="utf-8")
    resource = root / "resource" / "官服"
    (resource / "pipeline").mkdir(parents=True, exist_ok=True)
    (resource / "pipeline" / "maa_end_smoke.json").write_text(
        json.dumps({"MaaEndSmoke": {"recognition": "DirectHit", "action": "DoNothing"}}),
        encoding="utf-8",
    )
    return root


# --------------------------------------------------------------------------- #
# 驱动
# --------------------------------------------------------------------------- #

def payload_of(result) -> dict:
    if isinstance(result, Ok):
        return result.value if isinstance(result.value, dict) else {"output": result.value}
    return {}


def error_of(result) -> str:
    if isinstance(result, Err):
        return str(getattr(result.error, "message", result.error))
    return ""


async def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="maa-end-stub-"))
    shim = build_shim(workdir)
    sys.path.insert(0, str(shim))

    import plugin.sdk.plugin as sdk  # 必须在 shim 入 path 之后导入

    global Ok, Err
    Ok, Err = sdk.Ok, sdk.Err

    module = __import__("plugin.plugins.maa_end", fromlist=["MaaEndPlugin"])
    cls = module.MaaEndPlugin

    # ---------------------------------------------------------------- 静态
    manifest = tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8"))
    meta = manifest.get("plugin", {})
    check(meta.get("id") == "maa_end", "plugin.toml id 与目录名一致", str(meta.get("id")))
    check(
        meta.get("entry") == "plugin.plugins.maa_end:MaaEndPlugin",
        "entry 指向本模块的类",
        str(meta.get("entry")),
    )

    def entry_ids(*classes):
        found = {}
        for cls_ in classes:
            for name in dir(cls_):
                dec_meta = getattr(getattr(cls_, name), "__neko_meta__", None)
                if dec_meta is not None and dec_meta.kind == "entry":
                    found[dec_meta.kw.get("id") or name] = (cls_.__name__, name)
        return found

    routers_by_name = {r.__name__: r for r in cls.__routers__}
    ef_cls = routers_by_name["MaaEndRouter"]
    ids = entry_ids(ef_cls)
    check(len(ids) >= 5, "maaend 入口数量", "%d 个" % len(ids))
    all_ids = entry_ids(cls, *cls.__routers__)
    declared = list(all_ids)
    check(len(set(declared)) == len(declared), "全插件入口 id 唯一（四子系统合并后）",
          "%d 个" % len(declared))
    check(all(getattr(getattr(ef_cls, n), "__neko_meta__").kw.get("timeout") for _, n in ids.values()),
          "每个 maaend 入口都声明了 timeout（宿主看门狗）")

    import inspect

    lax = [eid for eid, (_, n) in ids.items()
           if not any(p.kind is inspect.Parameter.VAR_KEYWORD
                      for p in inspect.signature(getattr(ef_cls, n)).parameters.values())]
    check(not lax, "所有 maaend 入口接受 **_（宿主注入 _ctx）", ", ".join(lax))

    plugin = cls(StubCtx(PLUGIN_DIR, workdir / "storage", sdk._StubConfig({})))
    ef_tools = sorted(n for n in plugin.llm_tools if n.startswith("maa_"))
    # 核心工具必须在册；不做全量精确匹配——工具集会随版本增减,精确清单只会腐化
    core = {"maa_status", "maa_list_tasks", "maa_run_task", "maa_task_state",
            "maa_stop_task", "maa_decide"}
    check(
        core <= set(ef_tools),
        "对话期工具已注册（maaend 子系统）",
        ", ".join(ef_tools),
    )

    # ---------------------------------------------------------------- 探测层
    from plugin.plugins.maa_end.maaend.infra import probe as detect

    windows = detect.count_windows()
    check(windows > 0, "Win32 窗口枚举真实可用（桌面可见窗口）", "%d 个" % windows)
    no_match = detect.find_game_window("\x00", "\x00")
    check(no_match["found"] is False, "无匹配正则如实报告未找到")
    game = detect.find_game_window("UnityWndClass", "Endfield")
    if game["found"]:
        check(True, "检测到终末地游戏窗口在线", f"{game.get('title', '')[:30]} / {game.get('class', '')}")
    else:
        check(True, "终末地窗口未开（如实报告未找到）")
    any_window = detect.find_game_window(r".*", r".")
    check(any_window["found"] is True, "宽松正则能命中真实窗口",
          f"{any_window.get('title', '')[:30]}")

    # ---------------------------------------------------------------- 假安装树
    fake_root = build_fake_install(workdir)

    # 用配置把安装根指到假树
    settings = {"maa": {"maaend_dir": str(fake_root), "allow_write": False, "allow_run": False}}
    ctx = StubCtx(PLUGIN_DIR, workdir / "storage", sdk._StubConfig(settings))
    plugin = cls(ctx)

    # ---- 首跑前生命周期回归(2026-09-25 起线上 12 次崩溃的根因防线) -----
    # 崩溃史:shutdown/task_state/stop_task 在从未 run_task 前访问未初始化的
    # _runner → AttributeError。此处固化契约:首跑前三者都必须安全返回。
    pre = await invoke(plugin.maa_router, "on_shutdown")
    check(payload_of(pre).get("status") == "stopped",
          "首跑前 on_shutdown 安全返回(不抛 _runner AttributeError)")
    pre_state = await invoke(plugin.maa_router, "task_state")
    check(type(pre_state).__name__ == "Err",
          "首跑前 task_state 如实报错而非崩溃", str(getattr(pre_state, "error", ""))[:60])
    pre_stop = await invoke(plugin.maa_router, "stop_task")
    check(type(pre_stop).__name__ == "Ok" or type(pre_stop).__name__ == "Err",
          "首跑前 stop_task 安全返回(不崩溃)")

    startup = await invoke(plugin.maa_router, "on_startup")
    check(payload_of(startup).get("status") == "ready", "startup 就绪")

    status = payload_of(await invoke(plugin.maa_router, "status"))
    check(status.get("found") is True, "status 探测到假安装树", str(status.get("root"))[:60])
    check(status.get("runtime_ready") is True, "maafw 运行库存在性通过")
    check(isinstance(status.get("task_count"), int) and status["task_count"] == 1,
          "真实 JSON5 清单解析 + import 合并", str(status.get("task_count")))
    check(bool(status.get("interface")), "接口元信息（controller/resource/agent）", 
          str((status.get("interface") or {}).get("controllers")))

    tasks = payload_of(await invoke(plugin.maa_router, "list_tasks"))
    check(tasks.get("total") == 1, "import 子清单的任务被合并",
          json.dumps(tasks.get("tasks"), ensure_ascii=False)[:90])
    check((tasks.get("tasks") or [{}])[0].get("entry") == "MaaEndSmoke",
          "合并后的任务带 entry", str((tasks.get("tasks") or [{}])[0].get("entry")))

    # 闸门：allow_run=false
    gated = await invoke(plugin.maa_router, "run_task", name="随便", options_json="")
    check(isinstance(gated, Err) and "allow_run" in error_of(gated),
          "run_task 闸门关闭时拒绝", error_of(gated)[:70])

    # 放行后：未知任务在 spawn 之前就被拒绝（不启动 MaaPiCli）
    settings["maa"]["allow_run"] = True
    await invoke(plugin.maa_router, "on_config_change")
    unknown = await invoke(plugin.maa_router, "run_task", name="不存在的任务", options_json="")
    check(isinstance(unknown, Err) and "任务清单里没有" in error_of(unknown),
          "放行后未知任务仍在 spawn 前拒绝", error_of(unknown)[:80])

    # 排轴预设：数据码按 endaxistimeline.go 的 decodeEndAxisShareCode 逆向独立解一遍
    import base64 as _b64
    import gzip as _gz

    ea_list = payload_of(await invoke(plugin.maa_router, "endaxis", action="list"))
    check([p["name"] for p in ea_list.get("presets", [])] == ["开荒", "精英", "BOSS"],
          "排轴 list 给出三套预设", json.dumps(ea_list, ensure_ascii=False)[:90])
    for preset in ("开荒", "精英", "BOSS"):
        got = payload_of(await invoke(plugin.maa_router, "endaxis", action="code", preset=preset))
        code = got.get("code", "")
        try:  # Go: base64.RawURLEncoding（无 '='、- _ 字母表）→ gzip → JSON
            ok_alpha = re.fullmatch(r"[A-Za-z0-9_-]+", code) is not None
            root = json.loads(_gz.decompress(_b64.urlsafe_b64decode(code + "=" * (-len(code) % 4))))
            acts = [a for sc in root["scenarioList"] for t in sc["data"]["tracks"] for a in t["actions"]]
            valid = (ok_alpha and acts and len(root["scenarioList"][0]["data"]["tracks"]) == 4
                     and all(a["type"] in ("skill", "ultimate") and a["startTime"] >= 300
                             and isinstance(a["startTime"], int) and a["duration"] > 0 for a in acts))
        except Exception as exc:  # noqa: BLE001 - 测试里任何解码失败都算 FAIL
            valid, acts = False, [str(exc)]
        check(bool(valid), f"排轴数据码可按 Go 逻辑解码（{preset}）", "%d 个动作" % len(acts))
    small = payload_of(await invoke(plugin.maa_router, "endaxis", action="code", preset="开荒", members=2))
    root2 = json.loads(_gz.decompress(_b64.urlsafe_b64decode(small["code"] + "=" * (-len(small["code"]) % 4))))
    check(all(len(sc["data"]["tracks"]) == 2 for sc in root2["scenarioList"]),
          "排轴 members=2 只保留 1-2 号位", json.dumps(small.get("scenarios"), ensure_ascii=False))
    bad = await invoke(plugin.maa_router, "endaxis", action="code", preset="不存在")
    check(isinstance(bad, Err) and "没有预设" in error_of(bad), "排轴未知预设被拒绝", error_of(bad)[:60])
    ea_run = await invoke(plugin.maa_router, "endaxis", action="run", preset="BOSS")
    check(isinstance(ea_run, Err) and "RealTimeTask" in error_of(ea_run),
          "排轴 run 复用 run_task 校验（假清单无 RealTimeTask 即拒绝）", error_of(ea_run)[:70])

    # 运行器目录搭建（不 spawn）：exe/dll/interface 就位 + junction
    from plugin.plugins.maa_end.maaend.backends.pi_runner import PiRunner

    runner_dir = plugin.data_path("maa-pi-runner")
    runner = PiRunner(runner_dir, fake_root)
    prepare_report = runner.prepare()
    check((runner_dir / "MaaPiCli.exe").is_file()
          and (runner_dir / "MaaFramework.dll").is_file()
          and (runner_dir / "interface.json").is_file(),
          "运行器目录就绪（exe/dll/interface）",
          json.dumps(prepare_report, ensure_ascii=False)[:120])
    merged_opt = json.loads((runner_dir / "interface.json").read_text(encoding="utf-8"))["option"]
    check(merged_opt.get("某选项", {}).get("cases", [{}])[0].get("option") == ["子输入"],
          "合并版 interface 保留 case 子选项", json.dumps(merged_opt.get("某选项"), ensure_ascii=False))
    check(merged_opt.get("子输入", {}).get("type") == "input"
          and merged_opt["子输入"].get("inputs") == [{"name": "Code", "default": ""}],
          "合并版 interface 保留 input 型选项的 type/inputs",
          json.dumps(merged_opt.get("子输入"), ensure_ascii=False)[:120])
    check("type" not in merged_opt.get("热键", {}) and "hotkeys" not in merged_opt.get("热键", {}),
          "合并版 interface 剥掉 PI 不认识的 hotkey 类型", json.dumps(merged_opt.get("热键"), ensure_ascii=False))

    # PI 配置生成：控制器/资源/窗口/任务与选项映射
    cfg_path = runner.write_config(
        controller="Win32-Front",
        resource="官服",
        window_class="UnityWndClass",
        window_title="Endfield",
        tasks=[{"name": "冒烟任务", "option": [{"name": "某选项", "value": "某case"}]}],
    )
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    check(cfg["controller"]["name"] == "Win32-Front", "PI 配置控制器正确")
    check(cfg["win32"] == {"class_name": "UnityWndClass", "window_name": "Endfield"},
          "PI 配置窗口匹配正确", json.dumps(cfg["win32"], ensure_ascii=False))
    check(cfg["resource"] == "官服", "PI 配置资源正确")
    check(cfg["task"][0]["option"][0] == {"name": "某选项", "value": "某case"},
          "PI 配置任务与选项正确", json.dumps(cfg["task"], ensure_ascii=False)[:120])

    # ---------------------------------------------------------------- 陪玩辅助（三项参数）
    # 需求：三项参数可在设置面板切换；卡关辅助自动触发；精细建议受置信度门槛约束。
    cfg_view = payload_of(await invoke(plugin.maa_router, "assist", action="config"))
    opts = cfg_view.get("options") or {}
    check(opts.get("assist_mode") == ["宏观建议", "精细建议"],
          "assist_mode 选项与需求一致", str(opts.get("assist_mode")))
    check(opts.get("confidence_threshold") == [0.70, 0.80, 0.85, 0.90],
          "confidence_threshold 选项与需求一致", str(opts.get("confidence_threshold")))
    check(opts.get("consecutive_fail_threshold") == [2, 3, 5],
          "consecutive_fail_threshold 选项与需求一致", str(opts.get("consecutive_fail_threshold")))

    # plugin.toml 的默认值必须与需求一致
    defaults = cfg_view.get("config") or {}
    check(defaults.get("assist_mode") == "宏观建议"
          and defaults.get("confidence_threshold") == 0.85
          and defaults.get("consecutive_fail_threshold") == 3,
          "三项默认值与需求一致（宏观建议 / 0.85 / 3）", json.dumps(defaults, ensure_ascii=False))

    # 动态切换：改参数 → 写回配置 → 即时生效
    switched = payload_of(await invoke(
        plugin.maa_router, "assist", action="set",
        assist_mode="精细建议", confidence_threshold="0.70",
        consecutive_fail_threshold="5"))
    eff = switched.get("effective") or {}
    check(eff.get("assist_mode") == "精细建议"
          and eff.get("confidence_threshold") == 0.70
          and eff.get("consecutive_fail_threshold") == 5,
          "三项参数动态切换即时生效", json.dumps(eff, ensure_ascii=False))
    persisted = (settings.get("maa") or {})
    check(persisted.get("assist_mode") == "精细建议",
          "设置写回配置（config.update 段名做键）", str(persisted.get("assist_mode")))

    # 非法参数要如实拒绝，而不是静默接受
    bad = await invoke(plugin.maa_router, "assist", action="set", confidence_threshold="abc")
    check(isinstance(bad, Err), "非法 confidence_threshold 被拒绝", error_of(bad)[:60])
    empty = await invoke(plugin.maa_router, "assist", action="set")
    check(isinstance(empty, Err), "空 patch 被拒绝", error_of(empty)[:60])

    # 分层输出：同一批事件，宏观模式不给精细建议；精细模式 + 低门槛给出
    from plugin.plugins.maa_end.maaend import synthetic_events as se

    for scen, (scen_fn, expected) in se.SCENARIOS.items():
        res = payload_of(await invoke(
            plugin.maa_router, "assist", action="advice",
            events_json=json.dumps(scen_fn(), ensure_ascii=False)))
        items = res.get("results") or []
        if not items:
            check(False, f"advice 未解析出战斗（{scen}）")
            continue
        got = items[0].get("label")
        want = expected if expected else "unknown"
        check(got == want, f"advice 归因正确（{scen} → {want}）", str(got))
        check(bool(items[0].get("macro")), f"advice 给出宏观建议（{scen}）",
              str(items[0].get("macro"))[:50])
        check(isinstance(items[0].get("confidence"), float),
              f"advice 给出置信度（{scen}）", str(items[0].get("confidence")))
        check(res.get("source") == "paste", f"advice 标注 paste 来源（{scen}）",
              str(res.get("source")))

    bad_json = await invoke(plugin.maa_router, "assist", action="advice", events_json="{不是JSON")
    check(isinstance(bad_json, Err), "非法 events_json 被拒绝", error_of(bad_json)[:60])
    empty_json = await invoke(plugin.maa_router, "assist", action="advice", events_json="")
    check(isinstance(empty_json, Err), "空 events_json 且辅助未启动被拒绝", error_of(empty_json)[:60])

    # 空 events_json 回落实时缓冲：只读快照，不抢后台循环的事件
    class _FakeAssist:
        """只实现 advice 回落用到的成员（pending_snapshot / cfg.enabled）。"""

        def __init__(self, events, enabled=True):
            self._events = events
            self.cfg = SimpleNamespace(enabled=enabled)

        def pending_snapshot(self):
            return list(self._events)

    live_events = se.SCENARIOS["dps_insufficient"][0]()
    before = json.dumps(live_events, ensure_ascii=False, sort_keys=True)
    plugin.maa_router.state.assist = _FakeAssist(live_events)
    live_res = await invoke(plugin.maa_router, "assist", action="advice", events_json="")
    live_payload = payload_of(live_res)
    live_items = live_payload.get("results") or []
    check(isinstance(live_res, Ok) and live_payload.get("source") == "live"
          and bool(live_items) and live_items[0].get("label") == "dps_insufficient",
          "空 events_json 回落实时缓冲并归因正确",
          json.dumps(live_payload, ensure_ascii=False)[:90])
    check(json.dumps(live_events, ensure_ascii=False, sort_keys=True) == before,
          "live 回落不改写缓冲事件 dict")
    plugin.maa_router.state.assist = _FakeAssist([])
    empty_live = await invoke(plugin.maa_router, "assist", action="advice", events_json="")
    check(isinstance(empty_live, Err) and "缓冲" in error_of(empty_live),
          "实时缓冲为空如实报 Err", error_of(empty_live)[:60])
    plugin.maa_router.state.assist = _FakeAssist([], enabled=False)
    disabled_live = await invoke(plugin.maa_router, "assist", action="advice", events_json="")
    check(isinstance(disabled_live, Err) and "禁用" in error_of(disabled_live),
          "辅助已禁用如实报 Err", error_of(disabled_live)[:60])
    plugin.maa_router.state.assist = None
    no_assist = await invoke(plugin.maa_router, "assist", action="advice", events_json="")
    check(isinstance(no_assist, Err) and "未启动" in error_of(no_assist),
          "辅助未启动如实报 Err", error_of(no_assist)[:60])

    # 未启动时也要能出建议（陪聊默认形态：不依赖后台服务在跑）
    state = payload_of(await invoke(plugin.maa_router, "assist", action="state"))
    check("running" in state, "assist state 如实报告运行状态",
          json.dumps(state, ensure_ascii=False)[:90])

    # ---------------------------------------------------------------- Hosted UI 面板
    panel_toml = tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8"))
    ui_cfg = panel_toml.get("plugin", {}).get("ui", {})
    panels = ui_cfg.get("panel") or []
    check(bool(ui_cfg.get("enabled")), "plugin.toml 声明了 [plugin.ui] enabled")
    check(len(panels) == 1 and panels[0].get("entry") == "ui/panel.tsx",
          "声明了 TSX 面板 ui/panel.tsx", json.dumps(panels, ensure_ascii=False)[:90])
    ctx_id = (panels[0] or {}).get("context") if panels else ""
    check(ctx_id == "maa_end_assist", "面板 context 与 Python 侧一致", str(ctx_id))
    check((PLUGIN_DIR / "ui" / "panel.tsx").is_file(), "面板文件真实存在")

    # 三项参数的 Select 选项必须在面板里出现（且容器 overflow: visible）
    tsx = (PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
    check("宏观建议" in tsx and "精细建议" in tsx, "面板含 assist_mode 两个选项")
    check('"0.70"' in tsx and '"0.90"' in tsx, "面板含 confidence_threshold 四档")
    check('"2"' in tsx and '"5"' in tsx, "面板含 consecutive_fail_threshold 三档")
    check("overflow" in tsx and "visible" in tsx,
          "下拉容器声明 overflow: visible（弹出层不被裁）")
    check("props.api.call" in tsx, "面板走 props.api.call（禁止裸 api.）")
    check(not re.search(r"(?<!props\.)(?<!\.)\bapi\.call", tsx),
          "面板没有裸 api.call")
    check("ef_assist" in tsx, "面板调用 ef_assist 动作")

    # Python 侧 context 提供者可调用，且返回面板要的三个字段
    # 注意：@ui.context 处理器返回**裸 dict**（不是 Ok 包装），它就是 props.state
    ctx_payload = await invoke(plugin, "maa_end_assist")
    check(isinstance(ctx_payload, dict) and "config" in ctx_payload,
          "ui.context 返回 config（面板 props.state）",
          json.dumps(ctx_payload, ensure_ascii=False)[:90])
    ctx_cfg = (ctx_payload or {}).get("config") or {}
    check("assist_mode" in ctx_cfg and "confidence_threshold" in ctx_cfg
          and "consecutive_fail_threshold" in ctx_cfg,
          "ui.context 带三项参数", json.dumps(ctx_cfg, ensure_ascii=False))
    check(ctx_cfg.get("assist_mode") == "精细建议",
          "ui.context 反映改动后的参数（动态切换可见）", str(ctx_cfg.get("assist_mode")))

    # ------------------------------------------------- command-loop 钩子回归（v0.2.1）
    # 契约裁决 M3：单循环桩测对「startup 短循环杀任务」恒绿，这里做真双循环。
    # 旧代码上本块必红（无钩子属性 -> T1/T4 红；on_startup 含 create_task -> T2 红）。
    import inspect as _insp
    _hk = getattr(plugin, "_on_command_loop_start", None)
    check(callable(_hk) and _insp.iscoroutinefunction(_hk),
          "T1 主类有 async _on_command_loop_start 钩子（宿主 _async_command_loop 约定）")
    _src_s = _insp.getsource(type(plugin.maa_router).on_startup)
    check("_assist_start(" not in _src_s and "create_task(self._remind_loop" not in _src_s,
          "T2 on_startup 不再创建后台任务（改由钩子挂 command 循环）")
    _src_h = _insp.getsource(type(plugin.maa_router).on_command_loop_start)
    check("_assist_start" in _src_h and "_remind_loop" in _src_h
          and "_settings_snapshot" not in _src_h and "await self._current_settings" not in _src_h,
          "T3 钩子挂 assist+remind 且不 await config IPC（钩子窗口下行链路无读者）")
    _r = plugin.maa_router
    _r.state.settings = {"assist_poll_seconds": "1"}   # 1s 轮询，观测窗更短
    _res = {}

    def _worker() -> None:  # 主线程已有一个运行中的循环：临时循环只能在子线程里跑
        _loop2 = asyncio.new_event_loop()
        try:
            _loop2.run_until_complete(plugin._on_command_loop_start())
            _loop2.run_until_complete(asyncio.sleep(3.4))  # 同一循环跑 ~3 个 poll 周期
            _st = _r.state.assist.state() if _r.state.assist is not None else {}
            _res["polls"] = (_st.get("counters") or {}).get("polls", 0)
            _res["alive1"] = (_r.state.assist_task is not None
                              and not _r.state.assist_task.done()
                              and _r.state.remind_task is not None
                              and not _r.state.remind_task.done())
            # 幂等：第二次钩子调用（同 settings 缓存），不得重复建任务、不得打断已挂循环
            _loop2.run_until_complete(plugin._on_command_loop_start())
            _loop2.run_until_complete(asyncio.sleep(0.2))
            _res["alive2"] = (_r.state.assist_task is not None
                              and not _r.state.assist_task.done()
                              and _r.state.remind_task is not None
                              and not _r.state.remind_task.done())
        finally:
            _tks = [x for x in (_r.state.assist_task, _r.state.remind_task) if x is not None]
            for _tk in _tks:
                if not _tk.done():
                    _tk.cancel()
            try:
                _loop2.run_until_complete(asyncio.gather(*_tks, return_exceptions=True))
            except Exception:  # noqa: BLE001
                pass
            _loop2.close()
            _r.state.assist = None
            _r.state.assist_task = None
            _r.state.remind_task = None

    import threading as _threading
    _w = _threading.Thread(target=_worker, name="stub-command-loop", daemon=True)
    _w.start()
    _w.join(20)
    check(_res.get("polls", 0) >= 3 and _res.get("alive1", False),
          "T4 双循环：临时循环上钩子任务存活且 polls>=3（旧代码必红）",
          "polls=%s alive=%s" % (_res.get("polls"), _res.get("alive1")))
    check(_res.get("alive2", False), "T5 钩子重复调用幂等：任务不被打断", str(_res))

    # ------------------------------------------------ logs 新鲜度闸门回归（v0.2.2）
    # 夹具前置（契约 R3，缺一即假绿）：文件名 session_*.ndjson（rglob 命中）、
    # 每行 \n 收尾、裸事件带 type 能过 _unwrap、log_dir 目录先建再构造 reader、
    # ws 指死端口（先例 tests/test_assist_logic.py:166 _dead_ws_url）。
    import time as _time

    from plugin.plugins.maa_end.maaend import assist as _as

    def _mk_stale(tag: str, age: float, n: int):
        d = _stale_gate_dir / tag
        d.mkdir(parents=True, exist_ok=True)
        f = d / ("session_%s.ndjson" % tag)
        f.write_text("".join(json.dumps({"type": "damage", "session_id": "s-" + tag,
                                        "timestamp_ms": 1000 + i}) + "\n"
                            for i in range(n)), encoding="utf-8")
        os.utime(f, (_time.time() - age, _time.time() - age))
        return d, f, _as.ZoneReader(_as.AssistConfig(ws_url="ws://127.0.0.1:1/ws", log_dir=str(d)))

    _stale_gate_dir = Path(tempfile.mkdtemp(prefix="maa-end-stale-gate-"))
    _d6, _f6, _r6 = _mk_stale("fresh", 0, 3)
    try:
        _g6 = _r6.poll()
    finally:
        _r6.close()
    check(len(_g6) == 3 and _r6.source == "logs",
          "T6 新鲜文件首见仍回看尾部（WS 断连补口语义保住）",
          "got=%d src=%s" % (len(_g6), _r6.source))

    _d7, _f7, _r7 = _mk_stale("stale", 2 * 3600.0, 3)
    _sz7 = _f7.stat().st_size
    try:
        _g7 = _r7.poll()
        check(not _g7 and _r7._offsets.get(str(_f7)) == _sz7 and _r7.source != "logs",
              "T7 超龄文件首见不回看、偏移落到文件尾（旧代码必红）",
              "got=%d off=%s size=%d src=%s"
              % (len(_g7), _r7._offsets.get(str(_f7)), _sz7, _r7.source))
        _f7.open("a", encoding="utf-8").write(
            json.dumps({"type": "damage", "session_id": "s-stale", "timestamp_ms": 9000}) + "\n")
        _g8 = _r7._drain_logs()
    finally:
        _r7.close()
    check(len(_g8) == 1, "T8 跳回看不等于放弃该文件：新追加行照常读到", "got=%d" % len(_g8))
    check(_as.LOG_FRESH_SECONDS == 30 * 60.0,
          "T9 新鲜度门槛 = 30 分钟（与 PENDING_FLUSH_SECONDS 同数值不同语义，勿漂移）",
          str(_as.LOG_FRESH_SECONDS))

    # ---------------------------------------------------------------- 真实 MaaFramework（可选）
    fw_bin = os.environ.get("MAA_FW_BIN", "")
    ran_real = False
    if fw_bin and Path(fw_bin, "MaaFramework.dll").is_file():
        ran_real = True
        check(True, "真实运行库验证", fw_bin)
        from plugin.plugins.maa_end.maaend.backends import maa_fw as fw_mod

        try:
            core = fw_mod.MaaFw(fw_bin)
            version = core.version()
            check(bool(version), "MaaVersion() 返回版本", version)
        except Exception as exc:  # noqa: BLE001
            check(False, "真实运行库验证异常", f"{type(exc).__name__}: {exc}")
    else:
        check(True, "未提供 MAA_FW_BIN：跳过真实 DLL 验证")

    # ---------------------------------------------------------------- 报告
    lines = ["=" * 78, "game_assist / maaend 子系统自检报告", "=" * 78]
    for ok, label, detail in RESULTS:
        lines.append("[%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  -> " + detail) if detail else ""))
    lines.append("")
    lines.append("共 %d 项，失败 %d 项" % (len(RESULTS), sum(1 for r in RESULTS if not r[0])))
    lines.append("真实运行库验证: %s" % ("已执行" if ran_real else "跳过（未设 MAA_FW_BIN）"))
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[-5:]))
    return 1 if any(not r[0] for r in RESULTS) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(2)
