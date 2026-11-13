"""MaaPiCli（MaaFramework 官方 PI 运行器）适配层。

MaaPiCli 随 MaaEnd 的 `maafw/` 发布，支持 `-d` 直通模式：读取自身目录下
`config/maa_pi_config.json`（PI 配置：控制器、资源、任务与选项），按完整
ProjectInterface 语义执行（选项 case→流水线覆盖、agent 子进程、分组……，
PI v2.5.0）。交互菜单只在无 `-d` 时进入（源码 main.cpp：`if (directly) run()`）。

运行目录布局（本插件数据目录下 `maa-pi-runner/`，由 :meth:`PiRunner.prepare` 搭建）：

    maa-pi-runner/
    ├── MaaPiCli.exe          ← 从 MaaEnd 的 maafw/ 拷贝
    ├── *.dll                 ← 同上（MaaFramework 及其依赖）
    ├── interface.json        ← 从 MaaEnd 根拷贝（MaaEnd 更新后重拷）
    ├── resource → <安装>/resource   （NTFS junction，零拷贝）
    ├── agent    → <安装>/agent
    ├── tasks    → <安装>/tasks     ← interface.json 的 import 引用这些
    └── config/maa_pi_config.json ← 本插件按用户选择生成

不 junction 的目录：`maafw`（MaaPiCli 需要和自己同目录的运行库）、`data`
（MaaEnd 应用自己的数据，不能让 MaaPiCli 写进去）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

PI_CONFIG_REL = Path("config") / "maa_pi_config.json"
OUTPUT_LOG_REL = Path("maa-pi-output.log")


class RunnerError(RuntimeError):
    """运行器目录搭建或启动失败。"""


def _create_junction(target: Path, link: Path) -> None:
    """创建 NTFS junction（不需要管理员权限）。"""
    import _winapi  # noqa: PLC0415 - CPython 标准实现，Windows 专用

    _winapi.CreateJunction(str(target), str(link))


def _is_junction_to(link: Path, target: Path) -> bool:
    try:
        return os.path.realpath(link).lower() == os.path.realpath(target).lower()
    except OSError:
        return False


def _remove_link_or_tree(path: Path) -> None:
    # junction 用 rmdir 摘掉链接本身(悬空也行);rmtree 摘不掉 junction,还可能删到目标
    try:
        os.rmdir(path)
    except OSError:
        shutil.rmtree(path, ignore_errors=True)


def _sync_file(src: Path, dst: Path, report: dict[str, Any], label: str) -> None:
    """缺失或与源不同(大小/mtime,copy2 保留 mtime)才拷:换 MaaEnd 版本后必须刷新。"""
    import filecmp  # noqa: PLC0415

    if dst.is_file() and filecmp.cmp(src, dst, shallow=True):
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    report["copied"].append(label)


_WATCHER_PS1 = r"""
param([string]$Dir, [string]$StopFlag, [string]$ExitFile, [string]$OutFile)
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = Join-Path $Dir 'MaaPiCli.exe'
$psi.Arguments = '-d'
$psi.WorkingDirectory = $Dir
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true
$p = [System.Diagnostics.Process]::Start($psi)
$sw = [System.IO.StreamWriter]::new($OutFile)
$p.add_OutputDataReceived({ param($s,$e) if ($e.Data) { $sw.WriteLine($e.Data); $sw.Flush() } })
$p.add_ErrorDataReceived({ param($s,$e) if ($e.Data) { $sw.WriteLine($e.Data); $sw.Flush() } })
$p.BeginOutputReadLine()
$p.BeginErrorReadLine()
$killed = $false
try {
    while (-not $p.HasExited) {
        if (Test-Path $StopFlag) {
            $killed = $true
            & taskkill /PID $p.Id /T /F 2>$null | Out-Null
            break
        }
        Start-Sleep -Milliseconds 500
    }
    $p.WaitForExit()
}
finally {
    try { $sw.Close() } catch {}
    # 无论中途异常与否,exitcode 必落盘;主动停止写 STOPPED:<code> 哨兵,
    # 与异常退出区分(措辞别误导调用方)
    $val = "$($p.ExitCode)"
    if ($killed) { $val = "STOPPED:$val" }
    try { Set-Content -Path $ExitFile -Value $val } catch {}
}
"""


class PiRunner:
    """MaaPiCli 运行目录的搭建、配置生成与进程管理。"""

    def __init__(self, runner_dir: Path, install_root: str) -> None:
        self.dir = Path(runner_dir)
        self.install = Path(install_root)
        self.maafw_bin = self.install / "maafw"
        self.proc: subprocess.Popen | None = None
        self.log_path = self.dir / OUTPUT_LOG_REL
        self._log_handle = None
        self._timeout_timer: threading.Timer | None = None
        self._elevated_pid: int | None = None  # 提权模式哨兵(None=普通模式)

    # ---------------------------------------------------------------- 目录

    def prepare(self) -> dict[str, Any]:
        """搭/刷新运行目录。返回报告（拷了什么、链接了什么）。"""
        report: dict[str, Any] = {"copied": [], "linked": [], "skipped": []}
        self.dir.mkdir(parents=True, exist_ok=True)

        pi_exe = self.maafw_bin / "MaaPiCli.exe"
        if not pi_exe.is_file():
            raise RunnerError(f"MaaEnd 的 maafw/ 里没有 MaaPiCli.exe：{pi_exe}")

        # exe + 运行库：缺失或与当前安装不同才拷（换 MaaEnd 版本后必须刷新）。
        for src in self.maafw_bin.glob("*.dll"):
            _sync_file(src, self.dir / src.name, report, src.name)
        _sync_file(pi_exe, self.dir / pi_exe.name, report, pi_exe.name)

        # maafw 的子目录(plugins/ MaaAgentBinary/ 等)必须随迁:
        # MaaPiCli 的 PluginMgr 启动即加载 plugins/,目录缺失直接退出码 -1
        # (2026-09-27 实测:v2.29.0 maafw/plugins/MaaPluginDemo.dll 缺失即崩)。
        for sub in self.maafw_bin.iterdir():
            if not sub.is_dir():
                continue
            for src in sub.rglob("*"):
                if src.is_file():
                    rel = src.relative_to(self.maafw_bin)
                    _sync_file(src, self.dir / rel, report, rel.as_posix())

        # 顶层目录（resource/agent/tasks/locales/…）：junction，零拷贝。
        # 必须先于 interface.json:下面补 .exe 要经 agent/ junction 判文件存在,
        # 后建则首跑/换版本时补不上 → child_exec 无扩展名 → agent 秒退(2026-10-26 实测)。
        # maafw 必须随迁:go-service/cpp-algo 按 <exe父>/maafw/MaaFramework.dll
        # 解析库路径,缺 maafw/ 子目录则 agent 秒退(2026-09-27 实测 FTL)。
        # 平铺拷贝的 exe+dll 保留给 MaaPiCli 自身(同目录 DLL 搜索)。
        for child in self.install.iterdir():
            if not child.is_dir() or child.name in {"data", "config"}:
                continue
            link = self.dir / child.name
            # lexists:旧版本目录删掉后 junction 悬空,exists()/is_symlink() 都判 False
            if os.path.lexists(link):
                if _is_junction_to(link, child):
                    report["skipped"].append(child.name)
                    continue
                _remove_link_or_tree(link)
            try:
                _create_junction(child.resolve(), link)
                report["linked"].append(child.name)
            except (OSError, AttributeError):
                shutil.copytree(child, link, dirs_exist_ok=True)
                report["copied"].append(child.name + "/（拷贝）")

        # interface.json：生成「合并+规范化」版本，不直接拷原版。
        # 原版 interface.json 的 task 为空、任务分布带 import 引用，且导入文件
        # (tasks/setting/Keymap.json 等)含 PI 解析器不认的 option 形态(GUI 自己
        # 合并不走 MaaPiCli;直接拷原版 MaaPiCli 解析必挂,2026-09-27 实测)。
        # 这里用 catalog.load_catalog 做 import 合并(GUI 同款语义),再规范化
        # option 定义(去 type/label/inputs/hotkeys 等 GUI 扩展键,保留 cases/
        # default_case/pipeline_override)写给 MaaPiCli。
        interface = self.install / "interface.json"
        dst_iface = self.dir / "interface.json"
        if not interface.is_file():
            raise RunnerError(f"MaaEnd 根目录没有 interface.json：{interface}")
        merged = self._merged_interface(interface)
        # agent.child_exec 必须绝对路径:接口里是相对的 "agent/go-service",
        # MaaFramework 用 boost::process 拉起,相对名走 PATH 搜索解析不到
        # → process_error → SIGABRT(signal 22)秒退(2026-09-27 提权实测)。
        for agent in merged.get("agent") or []:
            ce = str(agent.get("child_exec") or "")
            if ce and not Path(ce).is_absolute():
                abs_ce = (self.dir / ce).resolve()
                # Windows 无扩展名时显式补 .exe(boost::process/CreateProcess 虽
                # 通常会自动补,但显式值消除一切歧义)
                if not abs_ce.is_file() and abs_ce.with_suffix(".exe").is_file():
                    abs_ce = abs_ce.with_suffix(".exe")
                agent["child_exec"] = str(abs_ce)
        payload = json.dumps(merged, ensure_ascii=False, indent=2)
        if (not dst_iface.is_file()) or dst_iface.read_text(encoding="utf-8") != payload:
            dst_iface.write_text(payload, encoding="utf-8")
            report["copied"].append("interface.json(合并版)")
        return report

    # ---------------------------------------------------------------- 合并 interface

    @staticmethod
    def _merged_interface(interface_path: Path) -> dict:
        """读原版 interface.json → import 合并 → PI 规范化。"""
        from .catalog import load_catalog

        cat = load_catalog(interface_path)

        def norm_case(case: dict) -> dict:
            out = {"name": case["name"]}
            if case.get("pipeline_override"):
                out["pipeline_override"] = case["pipeline_override"]
            # 子选项（如 AutoFightAxis=Yes → AutoFightAxisData），丢了就永远不会被激活
            if isinstance(case.get("option"), list):
                out["option"] = case["option"]
            return out

        def norm_option(value: dict) -> dict:
            out: dict = {}
            # type 缺省按 select 解析；input 型选项丢了 type/inputs 就无法代入用户输入。
            # 只放行 PI 枚举认识的四种：GUI 扩展的 "hotkey"（Keymap.json）会让 MaaPiCli 解析失败
            if value.get("type") in ("select", "input", "switch", "checkbox"):
                out["type"] = value["type"]
            if isinstance(value.get("inputs"), list):
                out["inputs"] = value["inputs"]
            if isinstance(value.get("description"), str):
                out["description"] = value["description"]
            cases = [
                norm_case(c)
                for c in (value.get("cases") or [])
                if isinstance(c, dict) and c.get("name")
            ]
            if cases:
                out["cases"] = cases
            if value.get("default_case"):
                out["default_case"] = value["default_case"]
            if value.get("pipeline_override"):
                out["pipeline_override"] = value["pipeline_override"]
            return out

        return {
            "interface_version": cat.get("interface_version"),
            "name": cat.get("name"),
            "version": cat.get("version"),
            "controller": cat.get("controller"),
            "resource": cat.get("resource"),
            "agent": cat.get("agent"),
            "task": cat.get("task") or [],
            "group": cat.get("group") or [],
            "option": {k: norm_option(v) for k, v in (cat.get("option") or {}).items()},
            "import": [],
        }

    # ---------------------------------------------------------------- 配置

    def write_config(
        self,
        *,
        controller: str,
        resource: str,
        window_class: str,
        window_title: str,
        tasks: list[dict[str, Any]],
        adb: dict[str, str] | None = None,
    ) -> Path:
        """写 PI 配置。tasks: [{name, option: [{name, value}...]}]（已按用户选择生成）。

        adb（ADB 控制器）：{"name","adb_path","address"} 三键必填
        （v5.14.0 Configuration::AdbConfig）；name 仅展示，对齐 MaaPiCli 手动
        输入格式 "{address}-{adb_path}"。
        """
        config: dict[str, Any] = {
            "controller": {"name": controller},
            "win32": {
                "class_name": window_class,
                "window_name": window_title,
            },
            "resource": resource,
            "task": tasks,
        }
        if adb is not None:
            config["adb"] = adb
        config_path = self.dir / PI_CONFIG_REL
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return config_path

    # ---------------------------------------------------------------- 进程

    def _is_elevated(self) -> bool:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            return False

    def spawn(
        self, timeout_hint: str = "", timeout_seconds: float = 0, need_elevate: bool = True
    ) -> subprocess.Popen:
        """启动 MaaPiCli（-d 直通）。

        timeout_seconds > 0 时起看门狗 Timer 到点整树终止（对齐 maa.run_timeout_seconds
        配置；MaaPiCli 会派生 AgentServer 子进程，超时必须 taskkill /T 才杀得干净）。

        need_elevate：控制器是否要管理员（Win32-Front 的 Seize 键鼠要；ADB 走
        adb 指令控制设备不要——非提权跑可重定向 stdout，退出码可靠）。

        提权路径:Win32-Front 控制器(Seize 键鼠)要管理员。此前依赖 MaaPiCli 自
        UAC 重启——但 Windows 提权启动 cwd 会落到 System32,提权实例找不到
        config/maa_pi_config.json → 空任务表 → exit 0 假完成(2026-09-27 实测)。
        现改为 PowerShell Start-Process -Verb RunAs -WorkingDirectory 保留 cwd,
        退出码经 exitcode.txt 落盘;stdout 不可重定向(Runas 限制),日志走
        debug/maafw.log(已足)。
        """
        self._poll_close()
        if self.proc is not None and self.proc.poll() is None:
            raise RunnerError("已有一个 MaaPiCli 任务在运行：先等它结束或 stop_task。")
        self._cancel_timeout_timer()
        exe = self.dir / "MaaPiCli.exe"
        if need_elevate and self._elevated_pid is None and not self._is_elevated():
            ec = self.dir / "exitcode.txt"
            if ec.is_file():
                ec.unlink()
            stop_flag = self.dir / "stop-flag.txt"
            if stop_flag.is_file():
                stop_flag.unlink()
            out_file = self.dir / "pi-elevated-out.log"
            watcher = self.dir / "pi-watcher.ps1"
            watcher.write_text(_WATCHER_PS1, encoding="utf-8-sig")
            # 提权 watcher 持有 MaaPiCli 子进程:能 Kill 自己的孩子(跨过
            # 「非提权杀不动提权」权限墙);插件 stop_task 只写 stop-flag 文件
            arglist = (
                "'-NoProfile','-ExecutionPolicy','Bypass',"
                f"-File,'{watcher}','-Dir','{self.dir}',"
                f"'-StopFlag','{stop_flag}','-ExitFile','{ec}','-OutFile','{out_file}'"
            )
            self.proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command",
                 "Start-Process powershell -Verb RunAs -WindowStyle Hidden -ArgumentList " + arglist],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._elevated_pid = -1  # 哨兵:提权模式,真实 pid 不可得
            return self.proc
        log = self.log_path.open("wb")  # noqa: SIM115 - 生命周期与进程绑定
        try:
            self.proc = subprocess.Popen(
                [str(exe), "-d"],
                cwd=str(self.dir),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            log.close()
            raise RunnerError(f"MaaPiCli 启动失败：{exc}") from exc
        self._log_handle = log
        if timeout_seconds > 0:
            timer = threading.Timer(
                timeout_seconds, lambda: self.stop() if self.running() else None
            )
            timer.daemon = True
            timer.start()
            self._timeout_timer = timer
        return self.proc

    def _poll_close(self) -> None:
        """进程退出后释放日志句柄，避免日志文件被本插件长期占用。"""
        if (
            self.proc is not None
            and self.proc.poll() is not None
            and self._log_handle is not None
        ):
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None

    def _cancel_timeout_timer(self) -> None:
        if self._timeout_timer is not None:
            self._timeout_timer.cancel()
            self._timeout_timer = None

    def running(self) -> bool:
        self._poll_close()
        if self._elevated_pid is not None:
            # 提权模式:exitcode.txt 出现 = 结束;否则按 MaaPiCli/go-service 进程存活判断
            if (self.dir / "exitcode.txt").is_file():
                return False
            for name in ("MaaPiCli.exe", "go-service.exe"):
                out = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {name}"],
                    capture_output=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ).stdout.decode(errors="replace")
                if name in out:
                    return True
            return False
        return self.proc is not None and self.proc.poll() is None

    def exit_code(self) -> int | None:
        self._poll_close()
        if self._elevated_pid is not None:
            ec_file = self.dir / "exitcode.txt"
            if ec_file.is_file():
                raw = ec_file.read_text(encoding="utf-8").strip()
                raw = raw.removeprefix("STOPPED:")
                try:
                    return int(raw or "-1")
                except ValueError:
                    return -1
            return None
        return self.proc.poll() if self.proc is not None else None

    def last_run_events(self) -> dict:
        """扫 debug/maafw.log 最后一个「MAA Process Start」会话,提取任务生命周期证据。

        exit==0 不可靠(SIGABRT 也 0,宿主 sig_handler 如此);成功判定必须落到
        task start / Succeeded 事件。
        """
        log = self.dir / "debug" / "maafw.log"
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {"started_entry": "", "succeeded": False, "failed": False}
        # 取最后一个会话段
        idx = text.rfind("MAA Process Start")
        segment = text[idx:] if idx >= 0 else text[-200000:]
        started = ""
        entry_marker = '"entry":"'
        for line in segment.splitlines():
            # 实际行形如: task start: [cb_detail={"entry":"DailyRewardStart",...}]
            if "task start:" in line and entry_marker in line:
                started = line.split(entry_marker, 1)[1].split('"', 1)[0]
                break
        return {
            "started_entry": started,
            "succeeded": "msg=Tasker.Task.Succeeded" in segment,
            "failed": "msg=Tasker.Task.Failed" in segment,
        }

    def output_tail(self, limit: int = 1200) -> str:
        self._poll_close()
        # 提权模式 stdout 不可重定向(Runas 限制),读框架日志代替
        source = (self.dir / "debug" / "maafw.log") if self._elevated_pid is not None else self.log_path
        try:
            if not source.is_file():
                return ""
            text = source.read_text(encoding="utf-8", errors="replace")
            return text[-limit:]
        except OSError:
            return ""

    def _pids_by_name(self, name: str) -> list[int]:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.decode(errors="replace")
        except Exception:  # noqa: BLE001
            return []
        pids = []
        for line in out.splitlines():
            parts = line.split('","')
            if len(parts) >= 2 and parts[0].strip('"') == name:
                try:
                    pids.append(int(parts[1].strip('"')))
                except ValueError:
                    continue
        return pids

    def stop(self) -> bool:
        """整树终止（MaaPiCli → AgentServer 子进程）。返回是否执行了终止动作。"""
        self._poll_close()
        if self.proc is None:
            return False
        self._cancel_timeout_timer()
        if self._elevated_pid is not None:
            # 提权模式:非提权本进程杀不动提权 MaaPiCli(ACCESS DENIED)。
            # 正道:写 stop-flag,由提权 watcher(它的父)taskkill /T 整树;
            # 此处等最多 12 秒确认进程消失,如实证结果。
            stop_flag = self.dir / "stop-flag.txt"
            try:
                stop_flag.write_text("stop", encoding="utf-8")
            except OSError:
                return False
            for _ in range(24):
                if not self.running():
                    return True
                time.sleep(0.5)
            return not self.running()
        if self.proc.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:  # noqa: BLE001 - taskkill 失败退化 terminate
                self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            return True
        return False
