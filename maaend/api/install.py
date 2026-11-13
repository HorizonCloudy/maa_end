"""安装探测、插件状态、任务清单、ADB、打开 App。

由 `maaend/router.py` 拆分而来（2026-11-04，见 docs/REFACTOR-PLAN.md 阶段 3）。
状态一律经 `self.state`（AppState）读写，不直接持有实例字段。
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from plugin.sdk.plugin import (
    Err,
    Ok,
    SdkError,
    plugin_entry,
    ui,
)

from ..backends.catalog import (
    load_catalog,
    meta_of,
    options_of,
    tasks_of,
)
from ..backends.maa_fw import MaaFw, MaaFwError
from ..constants import DOWNLOAD_URL  # noqa: E402
from ..infra.probe import (
    count_windows,
    find_game_install,
    find_install,
    same_exe,
    verify_enabled,
    window_exe,
)
from ..suite import LINKS
from ._helpers import game_window


class InstallAPI:
    """安装探测、插件状态、任务清单、ADB、打开 App。"""
    async def _install(self) -> dict[str, Any]:
        settings = await self._current_settings()
        return find_install(str(settings.get("maaend_dir") or ""))

    def _fw_or_err(self, install: dict[str, Any]):
        """加载运行库。成功返回 MaaFw（带缓存），失败返回 Err。"""
        if self.state.fw is not None and self.state.fw_bin == install["maafw_bin"]:
            return self.state.fw
        try:
            fw = MaaFw(install["maafw_bin"])
        except MaaFwError as exc:
            return Err(
                SdkError(
                    f"{exc}\nMaaEnd 未安装或安装不完整。下载：{DOWNLOAD_URL}\n"
                    f"或在配置 maa.maaend_dir 里写出安装根目录。"
                )
            )
        self.state.fw = fw
        self.state.fw_bin = install["maafw_bin"]
        return fw

    async def _gate_run(self) -> Err | None:
        settings = await self._current_settings()
        if settings.get("allow_run"):
            return None
        return Err(
            SdkError(
                "run_task 属于会接管游戏窗口输入的操作，本插件的 maa.allow_run 现在是 "
                "false，已拒绝执行。\n要放行：把安装目录下 config/plugin.toml 的 [maa] "
                "allow_run 改成 true，然后在插件面板重载本插件。"
            )
        )

    @staticmethod
    def _verify_window(settings: dict[str, Any], game: dict[str, Any]) -> str:
        """verify_game 生效时核验匹配到的窗口确属已安装的 Endfield.exe（防误绑其他 Unity 游戏）。

        返回拒绝原因；空串 = 放行。任务结果的回读核验在 task_state（maafw.log 的 Succeeded/Failed 事件）。
        """
        game_install = find_game_install()
        if not verify_enabled(str(settings.get("verify_game") or "auto"), game_install):
            return ""
        if not game_install["found"]:
            return ("maa.verify_game=on 但没检测到终末地安装（鹰角启动器 games/Arknights Endfield/"
                    "Endfield.exe）。装好游戏，或把 verify_game 改成 auto/off。")
        owner = window_exe(int(game.get("hwnd") or 0))
        if owner and not same_exe(owner, game_install["exe"]):  # 读不到进程路径（反作弊保护）不否决
            return (f"匹配到的窗口不属于已安装的终末地：窗口进程 {owner}，安装 {game_install['exe']}。"
                    "关掉那个窗口或收紧 maa.window_title_regex 后重试。")
        return ""

    def _catalog(self, install: dict[str, Any]) -> dict[str, Any]:
        path = install["interface_json"]
        if not path:
            raise ValueError("安装目录里没有 interface.json")
        return load_catalog(path)

    @ui.action(id="ef_status", label="状态")
    @plugin_entry(
        id="ef_status",
        name="状态",
        description="探测 MaaEnd 安装与运行库：安装路径、MaaFramework 版本、任务清单规模、"
        "游戏窗口是否在线、终末地本体安装与 verify_game 核验、写闸门状态。排查问题时先跑这个。",
        llm_result_fields=["summary", "found", "runtime_version", "task_count", "game_window",
                           "game_install", "verify_game", "allow_run"],
        timeout=60.0,
    )
    async def status(self, **_):
        settings = await self._current_settings()
        install = await self._install()
        payload: dict[str, Any] = {
            "found": install["found"],
            "root": install["root"],
            "app_exe": install["app_exe"],
            "maafw_bin": install["maafw_bin"],
            "runtime_ready": install["runtime_ready"],
            "interface_json": install["interface_json"],
            "resource_dirs": install["resource_dirs"],
            "allow_run": bool(settings.get("allow_run", False)),
        }
        summary_bits: list[str] = []

        # 运行库：装了就报真实版本（ctypes 仅读版本号；执行走 MaaPiCli）。
        if install["runtime_ready"]:
            try:
                fw = self._fw_or_err(install)
                if isinstance(fw, Err):
                    payload["runtime_error"] = str(fw.error.message)
                else:
                    payload["runtime_version"] = fw.version()
            except Exception as exc:  # noqa: BLE001
                payload["runtime_error"] = f"{type(exc).__name__}: {exc}"
        else:
            payload["runtime_error"] = "maafw 运行库不全（缺 MaaFramework.dll / MaaToolkit.dll）"

        # 游戏窗口：真实的 Win32 枚举。
        game = game_window(settings)
        payload["game_window"] = game
        payload["desktop_windows"] = count_windows()

        # 游戏本体安装 + verify_game（auto = 装了才核验）
        game_install = find_game_install()
        verify_mode = str(settings.get("verify_game") or "auto")
        payload["game_install"] = game_install
        payload["verify_game"] = {"mode": verify_mode,
                                  "enabled": verify_enabled(verify_mode, game_install)}
        if game["found"]:
            game["exe"] = window_exe(int(game["hwnd"]))
            # 读不到窗口进程（反作弊保护）= 未知；run_task 的 _verify_window 同样放行
            payload["game_window_verified"] = (same_exe(game["exe"], game_install["exe"])
                                               if game["exe"] else None)

        # 任务清单规模。
        task_count = 0
        if install["interface_json"]:
            try:
                catalog = self._catalog(install)
                meta = meta_of(catalog)
                tasks = tasks_of(catalog)
                payload["interface"] = meta
                task_count = len(tasks)
                payload["task_count"] = task_count
            except ValueError as exc:
                payload["interface_error"] = str(exc)

        if not install["found"]:
            payload["summary"] = (
                f"未找到 MaaEnd 安装（找过 {len(install['searched'])} 个常见位置）。"
                f"下载：{DOWNLOAD_URL}；装好后重载本插件，或在配置里写 maa.maaend_dir。"
            )
            return Ok(payload)

        summary_bits.append(f"根 {install['root']}")
        summary_bits.append("终末地已安装" if game_install["found"] else "未检测到终末地安装")
        if payload.get("runtime_version"):
            summary_bits.append(f"运行库 {payload['runtime_version']}")
        if task_count:
            summary_bits.append(f"{task_count} 个任务")
        summary_bits.append("游戏窗口在线" if game["found"] else "游戏窗口未开")
        summary_bits.append("任务已放行" if payload["allow_run"] else "任务未放行")
        if self.state.runner is not None and self.state.runner.running():
            payload["running"] = True
            summary_bits.append("MaaPiCli 执行中")
        else:
            payload["running"] = False
        payload["mode"] = self.state.mode or ""
        if self.state.m3_active:
            payload["running"] = True
            summary_bits.append("NitroGen 视觉代理执行中（M3）")

        # 套组速览（决策层 / NitroGen；跨插件失败如实标注，不拖垮 status）
        payload["links"] = LINKS
        if await self._suite_enabled():
            suite = self._suite_client(settings)
            layer_on = await suite.layer_enabled()
            n2 = suite.nitrogen_playing(await suite.nitrogen_status())
            payload["suite"] = {
                "decision_layer": suite.layer_id,
                "decision_layer_enabled": layer_on,
                "nitrogen_agent": suite.agent_id,
                "nitrogen_playing": n2,
            }
            if n2.get("running"):
                summary_bits.append(f"NitroGen 正在玩 {n2.get('target_process') or '?'}")
            elif layer_on is False:
                summary_bits.append("决策层未启用")
        payload["summary"] = "；".join(summary_bits)
        return Ok(payload)

    @plugin_entry(
        id="ef_list_tasks",
        name="任务清单",
        description="列出 MaaEnd interface.json 里声明的全部任务（名称、入口节点、描述、选项）。",
        llm_result_fields=["summary", "tasks"],
        timeout=60.0,
    )
    async def list_tasks(self, **_):
        install = await self._install()
        if not install["interface_json"]:
            return Err(
                SdkError(
                    f"未找到 MaaEnd 安装（或安装缺 interface.json）。下载：{DOWNLOAD_URL}"
                )
            )
        try:
            catalog = self._catalog(install)
        except ValueError as exc:
            return Err(SdkError(str(exc)))
        tasks = tasks_of(catalog)
        options = options_of(catalog)
        for task in tasks:
            task["option_definitions"] = {
                name: options.get(name) for name in task.get("option_names", []) if name in options
            }
        meta = meta_of(catalog)
        return Ok(
            {
                "summary": f"共 {len(tasks)} 个任务（接口 {meta['interface_name'] or '未命名'} "
                f"{meta['interface_version']}）",
                "tasks": tasks,
                "total": len(tasks),
                "interface": meta,
            }
        )

    @plugin_entry(
        id="ef_adb",
        name="ADB 设备",
        description="ADB 设备列表与截图（手机端/模拟器数据截取）。action=list 列设备"
        "（adb devices -l）；action=screenshot 对 serial 截图（exec-out screencap），"
        "PNG 存 data/adb/ 并返回路径与大小。adb 路径由 maa.adb_path 配置（默认 PATH 上的 adb）。",
        timeout=30.0,
    )
    async def adb_tools(
        self,
        action: Annotated[str, "list / screenshot"] = "list",
        serial: Annotated[str, "screenshot 时的设备地址"] = "",
        **_,
    ):
        import subprocess

        settings = await self._current_settings()
        adb = str(settings.get("adb_path") or "adb")

        def _run(*args: str) -> bytes:
            return subprocess.run(
                [adb, *args], capture_output=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout

        if str(action) == "screenshot":
            serial = (serial or "").strip()
            if not serial:
                return Err(SdkError("serial 不能为空：先用 action=list 查设备。"))
            try:
                png = _run("-s", serial, "exec-out", "screencap", "-p")
            except OSError as exc:
                return Err(SdkError(f"adb 不可用（{adb}）：{exc}"))
            if len(png) < 8 or png[:4] != b"\x89PNG":
                return Err(SdkError(f"设备 {serial} 返回的不是 PNG：设备可能离线/未授权（adb: {adb}）"))
            out_dir = self.data_path("adb")
            out_dir.mkdir(parents=True, exist_ok=True)
            import time as _t

            path = out_dir / f"shot_{_t.strftime('%Y%m%d_%H%M%S')}.png"
            path.write_bytes(png)
            return Ok({"ok": True, "serial": serial, "size": len(png), "path": str(path)})

        # action=list
        try:
            raw = _run("devices", "-l")
        except OSError as exc:
            return Err(SdkError(f"adb 不可用（{adb}）：装好 adb 或在配置 maa.adb_path 写绝对路径。{exc}"))
        devices = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            fields = line.split()
            if len(fields) < 2 or fields[0] == "List":
                continue
            dev = {"serial": fields[0], "state": fields[1]}
            for f in fields[2:]:
                if f.startswith("product:"):
                    dev["product"] = f.split(":", 1)[1]
                elif f.startswith("model:"):
                    dev["model"] = f.split(":", 1)[1]
            devices.append(dev)
        return Ok({
            "ok": True, "adb": adb, "total": len(devices), "devices": devices,
            "note": "run_task 走手机端：controller=ADB，adb_serial=设备地址。" if devices
            else "没有在线设备：先启动模拟器/连上手机（并开 USB 调试）。",
        })

    @plugin_entry(
        id="ef_open_app",
        name="打开 MaaEnd",
        description="启动 MaaEnd 应用本体（ automation 仍在 GUI 里操作时的备用入口）。",
        timeout=30.0,
    )
    async def open_app(self, **_):
        install = await self._install()
        if not install["app_exe"]:
            return Err(
                SdkError(f"未找到 MaaEnd 安装，无法启动。下载：{DOWNLOAD_URL}")
            )
        try:
            os.startfile(install["app_exe"])  # noqa: S606 - 用户明确要求的 GUI 启动
        except OSError as exc:
            return Err(SdkError(f"启动失败：{exc}"))
        return Ok({"launched": install["app_exe"]})
