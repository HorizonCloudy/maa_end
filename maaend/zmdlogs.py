"""maa_end ZMDLogs 战斗事件流 —— 插件侧子进程管理器。

ZMDLogs（https://github.com/deepdarkssj/ZMDLogs，MIT）通过网卡层抓包 + RSA 解密 +
protobuf 解析，还原出战斗事件（伤害/连携/buff），再用本地 websocket 广播给订阅方。
本模块**不把 ZMDLogs 源码并进插件**，只按用户配置把它当子进程拉起，
和 OCR sidecar 同一范式：源码/exe 留在用户自己指定的目录。

两种启动模式（`zmdlogs_launch_mode`）：
- ``source``（后备）：从源码树跑 `python -m endfieldlogs serve --no-overlay`。
  需要外部 RSA 私钥（仓库根 `rsa_keys.txt` 或 `src/endfieldlogs/embedded_keys.py`）。
  上游 README 明确不提供该文件，缺它时子进程会 FileNotFoundError 秒退。
- ``exe``（默认，推荐）：直接拉起预构建的 ``EndfieldLogs.exe``（Releases 解压版）。
  官方预构建包把密钥编译进了加密的 PyInstaller 归档，**开箱即用、不需要外部私钥**。
  这是本机唯一能真正跑通的形态——源码树因缺密钥无法启动。
  注意：该 exe 是**纯 GUI 应用**，忽略所有命令行参数（`serve` 子命令不存在），
  配置全部读同目录的 `endfieldlogs.gui.json`；且带**单实例锁**——已有实例在跑时
  再次拉起会立刻以 exit -1 退出。所以 exe 模式必须：①不带参数启动；
  ②「WS 端口已在监听」就视为成功（可能是用户手动开的那个实例），不重复拉起。

为什么单独拉起而不是直接 import：
- 插件宿主是 Nuitka 冻结 exe，ZMDLogs 依赖链（dpkt/psutil/pycryptodome/
  protobuf/websockets）在宿主进程内不可靠；
- ZMDLogs 需要 Npcap 与管理员权限抓包，与插件其余部分（只读截图识别）的
  权限模型不同，必须能独立关掉。

安全边界：`zmdlogs_enabled` 闸门默认关闭——它要在网卡层抓游戏流量。关掉时本模块
完全不启动任何进程，也不开任何端口。

仅依赖标准库。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

#: ZMDLogs 默认 websocket 端口（与其 cli.py serve --ws-port 默认值一致）。
DEFAULT_WS_PORT = 29325

#: Npcap 的安装痕迹。缺失时抓包不可用，如实报告而不是假装能跑。
NPCAP_MARKERS = (
    r"C:\Windows\System32\Npcap\Npcap.dll",
    r"C:\Windows\SysWOW64\Npcap\wpcap.dll",
    r"C:\Program Files\Npcap",
)

#: ZMDLogs 源码目录的候选位置（留空配置时的自动探测）。
REPO_CANDIDATES = (
    "%USERPROFILE%/ZMDLogs",
    "%USERPROFILE%/zmdlogs",
    "%USERPROFILE%/Desktop/ZMDLogs",
    "%LOCALAPPDATA%/ZMDLogs",
)

#: 预构建 EndfieldLogs.exe 的候选位置（exe 模式留空配置时的自动探测）。
EXE_CANDIDATES = (
    "%USERPROFILE%/Downloads/EndfieldLogs/EndfieldLogs.exe",
    "%USERPROFILE%/Desktop/EndfieldLogs/EndfieldLogs.exe",
    "%LOCALAPPDATA%/ZMDLogs/EndfieldLogs.exe",
    "%USERPROFILE%/ZMDLogs/EndfieldLogs.exe",
)

#: 合法的启动模式。
LAUNCH_MODE_SOURCE = "source"
LAUNCH_MODE_EXE = "exe"


class ZmdLogsError(RuntimeError):
    """子进程拉起或通信失败。"""


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(str(path).replace("/", os.sep))).expanduser()


def find_repo(explicit: str = "") -> Path | None:
    """定位 ZMDLogs 源码目录。判据是里面有 `src/endfieldlogs/cli.py`，
    而不是「目录存在」——空目录不该被当成可用。"""
    if explicit:
        cand = _expand(explicit)
        return cand if (cand / "src" / "endfieldlogs" / "cli.py").is_file() else None
    for pattern in REPO_CANDIDATES:
        cand = _expand(pattern)
        try:
            if (cand / "src" / "endfieldlogs" / "cli.py").is_file():
                return cand
        except OSError:
            continue
    return None


def find_exe(explicit: str = "") -> Path | None:
    """定位预构建的 EndfieldLogs.exe（exe 模式）。

    判据是「这是一个名为 EndfieldLogs.exe 的文件」，而不是「路径存在」——
    exe 模式下它是唯一被拉起的可执行体，找错就等于拉起别的程序。
    """
    def _ok(p: Path) -> bool:
        return p.is_file() and p.name.lower() == "endfieldlogs.exe"

    if explicit:
        cand = _expand(explicit)
        if _ok(cand):
            return cand
        # 允许用户直接给目录
        if cand.is_dir() and _ok(cand / "EndfieldLogs.exe"):
            return cand / "EndfieldLogs.exe"
        return None
    for pattern in EXE_CANDIDATES:
        cand = _expand(pattern)
        try:
            if _ok(cand):
                return cand
        except OSError:
            continue
    return None


def normalize_mode(mode: str, exe: str = "") -> str:
    """把配置里的启动模式规整成合法值。

    - 显式 ``exe`` / ``source`` 原样返回；
    - 空/非法：若配了 exe 路径则按 exe，否则按 source（保守，保持旧行为）。
    """
    m = str(mode or "").strip().lower()
    if m in (LAUNCH_MODE_EXE, LAUNCH_MODE_SOURCE):
        return m
    return LAUNCH_MODE_EXE if str(exe or "").strip() else LAUNCH_MODE_SOURCE


def npcap_present() -> bool:
    """Npcap 是否装了（抓包前提）。任一标记命中即算有。"""
    return any(Path(p).exists() for p in NPCAP_MARKERS)


def rsa_key_path(repo: Path, explicit: str = "") -> Path | None:
    """定位 RSA 私钥。ZMDLogs 支持两种形态：仓库根的 rsa_keys.txt，或
    src/endfieldlogs/embedded_keys.py 内嵌常量。二者都没有时返回 None。"""
    if explicit:
        cand = _expand(explicit)
        if cand.is_file():
            return cand
    root_key = repo / "rsa_keys.txt"
    if root_key.is_file():
        return root_key
    if (repo / "src" / "endfieldlogs" / "embedded_keys.py").is_file():
        return repo / "src" / "endfieldlogs" / "embedded_keys.py"
    return None


def _venv_python(repo: Path, explicit: str = "") -> str:
    """选解释器：显式配置 > 仓库 .venv > PATH 上的 python。

    优先 venv：ZMDLogs 的 dpkt/protobuf 等依赖装在那里，系统 python 未必有。
    """
    if explicit:
        cand = _expand(explicit)
        if cand.is_file():
            return str(cand)
    for name in (".venv/Scripts/python.exe", "venv/Scripts/python.exe"):
        cand = repo / name
        if cand.is_file():
            return str(cand)
    return sys.executable or "python"


class ZmdLogsRunner:
    """按需拉起 ZMDLogs `serve --no-overlay`，并如实报告它的存活状态。

    生命周期与 OCR sidecar 一致：懒启动、进程死了不自动重启（避免用户没开游戏时
    反复拉起抓包），`ensure_running()` 由上层在明确需要时调用，`stop()` 收尾。
    """

    def __init__(
        self,
        repo_dir: str = "",
        python_exe: str = "",
        ws_port: int = DEFAULT_WS_PORT,
        log_dir: str = "",
        rsa_key: str = "",
        enabled: bool = False,
        log_level: str = "INFO",
        exe_path: str = "",
        launch_mode: str = "",
    ) -> None:
        self.repo_dir = str(repo_dir or "")
        self._python_exe_cfg = str(python_exe or "")
        self.ws_port = int(ws_port or DEFAULT_WS_PORT)
        self._log_dir = str(log_dir or "")
        self._rsa_key_cfg = str(rsa_key or "")
        self.enabled = bool(enabled)
        self.log_level = str(log_level or "INFO")
        self._exe_cfg = str(exe_path or "")
        self.launch_mode = normalize_mode(launch_mode, self._exe_cfg)

        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._log_tail: list[str] = []
        self._started_at: float = 0.0
        self._last_error: str = ""

    # ------------------------------------------------------------- 探测

    def preflight(self) -> dict[str, Any]:
        """只做探测，不启动任何东西。给 status 入口如实汇报用。

        按启动模式给不同的就绪判据：
        - ``exe``：只要有可用的 EndfieldLogs.exe 即可（密钥已编译进预构建包），
          不再要求外部 RSA 私钥、源码目录或 venv；
        - ``source``：要求源码目录 + RSA 私钥 +（Npcap）。
        """
        info: dict[str, Any] = {
            "enabled": self.enabled,
            "launch_mode": self.launch_mode,
            "repo_found": False,
            "repo_dir": "",
            "exe_found": False,
            "exe_path": "",
            "npcap": npcap_present(),
            "rsa_key_found": False,
            "rsa_key_path": "",
            "python": "",
            "python_ok": False,
            "ready": False,
            "blocking": [],
        }

        if self.launch_mode == LAUNCH_MODE_EXE:
            exe = find_exe(self._exe_cfg)
            info["exe_found"] = exe is not None
            info["exe_path"] = str(exe) if exe else ""
            if exe is None:
                info["blocking"].append(
                    "未找到 EndfieldLogs.exe（zmdlogs_exe 留空时只探测常见位置："
                    "Downloads/Desktop/AppData 下的 EndfieldLogs 目录）"
                )
            if not info["npcap"]:
                info["blocking"].append("未检测到 Npcap（网卡层抓包前提，需 WinPcap 兼容模式）")
            info["ready"] = not info["blocking"]
            return info

        # ---- source 模式 ----
        repo = find_repo(self.repo_dir)
        info["repo_found"] = repo is not None
        info["repo_dir"] = str(repo) if repo else ""
        if repo is None:
            info["blocking"].append(
                "未找到 ZMDLogs 源码目录（zmdlogs_repo 留空时只探测常见位置）"
            )
            return info

        key = rsa_key_path(repo, self._rsa_key_cfg)
        info["rsa_key_found"] = key is not None
        info["rsa_key_path"] = str(key) if key else ""
        if key is None:
            info["blocking"].append(
                "缺 RSA 私钥：源码模式需要仓库根 rsa_keys.txt，或 "
                "src/endfieldlogs/embedded_keys.py（上游不提供；改用 exe 模式可绕过）"
            )

        if not info["npcap"]:
            info["blocking"].append("未检测到 Npcap（网卡层抓包前提，需 WinPcap 兼容模式）")

        exe = _venv_python(repo, self._python_exe_cfg)
        info["python"] = exe
        info["python_ok"] = Path(exe).is_file() or bool(exe)
        info["ready"] = not info["blocking"]
        return info

    # ------------------------------------------------------------- 进程

    def _build_cmd(self) -> tuple[list[str], Path]:
        """按模式拼命令行与工作目录。

        exe 模式：**不带任何参数**拉起预构建 EndfieldLogs.exe。它是纯 GUI 应用，
        忽略命令行（没有 serve 子命令），ws_port/log_dir 全读同目录的
        endfieldlogs.gui.json；cwd 落在 exe 目录即可。

        source 模式：`python -X utf8 -m endfieldlogs serve --no-overlay`。
        cwd 必须落在仓库里：-m endfieldlogs 依赖 src 在 sys.path，
        且 bundle_root() 会按 __file__ 反推 jsondata/data 的位置。
        """
        if self.launch_mode == LAUNCH_MODE_EXE:
            exe = find_exe(self._exe_cfg)
            if exe is None:
                raise ZmdLogsError("未找到 EndfieldLogs.exe")
            # 该 exe 忽略命令行，只认自己的 gui.json；这里刻意传空参数列表。
            return [str(exe)], exe.parent

        repo = find_repo(self.repo_dir)
        if repo is None:
            raise ZmdLogsError("未找到 ZMDLogs 源码目录")
        exe = _venv_python(repo, self._python_exe_cfg)
        cmd = [
            exe, "-X", "utf8", "-m", "endfieldlogs",
            "serve", "--no-overlay",
            "--ws-port", str(self.ws_port),
            "--log-level", self.log_level,
        ]
        logs = _expand(self._log_dir) if self._log_dir else repo / "logs"
        cmd += ["--log-dir", str(logs)]

        key = rsa_key_path(repo, self._rsa_key_cfg)
        if key is not None:
            cmd += ["--rsa-key-txt", str(key)]
        return cmd, repo

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ws_open(self) -> bool:
        """探测 WS 端口是否在监听。子进程起来了但端口没开=还在加载或已异常退出。"""
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.35)
        try:
            probe.connect(("127.0.0.1", self.ws_port))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def start(self) -> dict[str, Any]:
        """拉起子进程。已活着就如实报告，不重复拉起。"""
        with self._lock:
            if self.running():
                return {"ok": True, "already_running": True, "pid": self._proc.pid}
            if not self.enabled:
                return {"ok": False, "error": "zmdlogs_enabled=false，事件流闸门关闭"}

            info = self.preflight()
            if not info["ready"]:
                return {"ok": False, "error": "前置未满足：" + "；".join(info["blocking"]),
                        "preflight": info}

            # exe 模式带单实例锁：若 WS 端口已在监听，说明已有一个实例（可能是用户
            # 手动开的 GUI）在提供事件流——直接复用，不再拉起，否则新进程必退 -1。
            if self.launch_mode == LAUNCH_MODE_EXE and self.ws_open():
                return {"ok": True, "already_running": True, "reused_external": True,
                        "pid": 0, "ws_url": self.ws_url(),
                        "note": "检测到已有实例在监听 WS 端口，直接复用"}

            try:
                cmd, cwd = self._build_cmd()
            except ZmdLogsError as exc:
                return {"ok": False, "error": str(exc)}

            env = dict(os.environ)
            if self.launch_mode == LAUNCH_MODE_SOURCE:
                # 源码模式：-m endfieldlogs 依赖 src 在 sys.path
                env["PYTHONPATH"] = os.pathsep.join(
                    [str(cwd / "src"), env.get("PYTHONPATH", "")]
                ).rstrip(os.pathsep)
            try:
                self._proc = subprocess.Popen(
                    cmd, cwd=str(cwd), env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                self._proc = None
                return {"ok": False, "error": "启动失败: " + str(exc)[:160]}
            self._started_at = time.time()
            self._log_tail.clear()
            self._last_error = ""
            self._pump_log()
            return {"ok": True, "already_running": False, "pid": self._proc.pid,
                    "ws_url": self.ws_url()}

    def _pump_log(self) -> None:
        """后台抽干 stderr 到环形缓冲。

        ZMDLogs 的报错（Npcap 缺失、密钥加载失败）只出现在它的 stderr，
        不接出来的话 status 里就只能报「进程死了」，对用户毫无用处。
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return

        def _run() -> None:
            try:
                for raw in proc.stderr:  # type: ignore[union-attr]
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if not line:
                        continue
                    with self._lock:
                        self._log_tail.append(line)
                        if len(self._log_tail) > 60:
                            del self._log_tail[:-60]
            except (OSError, ValueError):
                pass

        threading.Thread(target=_run, name="zmdlogs-stderr", daemon=True).start()

    def stop(self) -> dict[str, Any]:
        """终止子进程。没跑过也安全返回（首跑前调用不报错）。"""
        with self._lock:
            if self._proc is None:
                return {"ok": True, "was_running": False}
            if self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    try:
                        # 与 stream.stop() 同理：宿主关闭宽限只有 1.5s，
                        # 这里的等待不能超过它，否则会记 "didn't stop gracefully"。
                        self._proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                except OSError as exc:
                    self._last_error = str(exc)[:160]
            self._proc = None
            return {"ok": True, "was_running": True}

    # ------------------------------------------------------------- 对外

    def ws_url(self) -> str:
        return "ws://127.0.0.1:%d/ws" % self.ws_port

    def resolved_log_dir(self) -> str:
        """实际使用的 logs 目录（子进程 --log-dir 落点）。"""
        if self._log_dir:
            return _expand(self._log_dir).as_posix()
        if self.launch_mode == LAUNCH_MODE_EXE:
            exe = find_exe(self._exe_cfg)
            return str(exe.parent / "logs") if exe else ""
        repo = find_repo(self.repo_dir)
        return str(repo / "logs") if repo else ""

    def tail(self, lines: int = 12) -> list[str]:
        with self._lock:
            return list(self._log_tail[-lines:])

    def state(self) -> dict[str, Any]:
        """汇总状态。给 status 入口与面板用。"""
        proc = self._proc
        alive = self.running()
        # 端口探测：自己有子进程时看它；没子进程时也探一次——exe 模式允许
        # 复用用户手动开的实例（外部 WS 依然可用，只是不归本插件管进程）。
        if alive:
            ws_open = self.ws_open()
        else:
            ws_open = self.ws_open()
        return {
            "enabled": self.enabled,
            "launch_mode": self.launch_mode,
            "running": alive,
            "external": (not alive) and ws_open,
            "pid": proc.pid if (proc is not None and alive) else 0,
            "uptime_seconds": round(time.time() - self._started_at, 1) if alive and self._started_at else 0.0,
            "ws_url": self.ws_url(),
            "ws_open": ws_open,
            "exit_code": (proc.poll() if proc is not None and not alive else 0),
            "log_dir": self.resolved_log_dir(),
            "log_tail": self.tail(),
            "last_error": self._last_error,
            "preflight": self.preflight(),
        }
