#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zmdlogs.py 纯逻辑自检：探测 / 命令拼装 / 闸门 / 启停 / 降级。

跑法：python tests/test_zmdlogs_logic.py   （零依赖，不需要真的抓包）

不启动真实子进程：用一个假的 "ZMDLogs 仓库"（只有 src/endfieldlogs/cli.py 和
.venv/Scripts/python.exe）来验证命令拼装与前置判定，避免测试依赖真实抓包环境。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "maaend"))

from zmdlogs import (  # noqa: E402
    LAUNCH_MODE_EXE,
    LAUNCH_MODE_SOURCE,
    ZmdLogsRunner,
    find_exe,
    find_repo,
    normalize_mode,
    npcap_present,
    rsa_key_path,
)


def _fake_repo(root: Path, *, with_key: bool = False, with_venv: bool = True) -> Path:
    """造一个形状与真实仓库一致的假目录。"""
    (root / "src" / "endfieldlogs").mkdir(parents=True, exist_ok=True)
    (root / "src" / "endfieldlogs" / "cli.py").write_text("# fake\n", encoding="utf-8")
    if with_key:
        (root / "rsa_keys.txt").write_text("=== CLIENT_NET_RSA_PRIVATE_KEY ===\nx\n", encoding="utf-8")
    if with_venv:
        venv = root / ".venv" / "Scripts"
        venv.mkdir(parents=True, exist_ok=True)
        (venv / "python.exe").write_bytes(b"MZ")  # 只需 is_file() 为真
    return root


def test_find_repo() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        good = _fake_repo(base / "ZMDLogs")

        assert find_repo(str(good)) == good
        assert find_repo(str(base / "nope")) is None, "空目录不该被当成仓库"

        # 判据是 cli.py 存在，不是目录存在
        (base / "empty").mkdir()
        assert find_repo(str(base / "empty")) is None


def test_rsa_key_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)

        plain = _fake_repo(base / "plain", with_key=True)
        assert rsa_key_path(plain) == plain / "rsa_keys.txt"

        embedded = _fake_repo(base / "embedded")
        (embedded / "src" / "endfieldlogs" / "embedded_keys.py").write_text("K=''\n", encoding="utf-8")
        assert rsa_key_path(embedded) == embedded / "src" / "endfieldlogs" / "embedded_keys.py"

        bare = _fake_repo(base / "bare")
        assert rsa_key_path(bare) is None, "两种形态都没有时必须如实返回 None"


def test_preflight_reports_blockers() -> None:
    with tempfile.TemporaryDirectory() as td:
        bare = _fake_repo(Path(td) / "bare")

        r = ZmdLogsRunner(repo_dir=str(bare), enabled=True)
        pf = r.preflight()
        assert pf["repo_found"] is True
        assert pf["rsa_key_found"] is False
        assert pf["ready"] is False
        assert any("RSA" in b for b in pf["blocking"]), pf["blocking"]

        # 闸门关着时即便前置齐了也不该 ready（不该误导用户以为能跑）
        keyed = _fake_repo(Path(td) / "keyed", with_key=True)
        r2 = ZmdLogsRunner(repo_dir=str(keyed), enabled=False)
        assert r2.preflight()["enabled"] is False

        # 仓库都找不到
        r3 = ZmdLogsRunner(repo_dir=str(Path(td) / "missing"), enabled=True)
        pf3 = r3.preflight()
        assert pf3["repo_found"] is False and pf3["ready"] is False
        assert pf3["blocking"], "找不到仓库必须给出原因"


def test_build_cmd() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        repo = _fake_repo(base / "ZMDLogs", with_key=True)
        (repo / "rsa_keys.txt").write_text("=== CLIENT_NET_RSA_PRIVATE_KEY ===\nx\n", encoding="utf-8")

        r = ZmdLogsRunner(repo_dir=str(repo), enabled=True, ws_port=29401, log_level="DEBUG")
        cmd, cwd = r._build_cmd()

        assert cwd == repo, "cwd 必须在仓库里，-m endfieldlogs 才找得到 src"
        assert cmd[0].endswith("python.exe"), cmd[0]
        assert "-m" in cmd and "endfieldlogs" in cmd
        assert "serve" in cmd
        assert "--no-overlay" in cmd, "插件只要事件流，不要悬浮窗"
        assert cmd[cmd.index("--ws-port") + 1] == "29401"
        assert cmd[cmd.index("--log-level") + 1] == "DEBUG"
        assert cmd[cmd.index("--rsa-key-txt") + 1] == str(repo / "rsa_keys.txt")
        assert cmd[cmd.index("--log-dir") + 1] == str(repo / "logs")

        # 自定义 log_dir 要被采纳
        custom = base / "mylogs"
        r2 = ZmdLogsRunner(repo_dir=str(repo), log_dir=str(custom))
        cmd2, _ = r2._build_cmd()
        assert cmd2[cmd2.index("--log-dir") + 1] == str(custom)

        # 没密钥时就不该硬塞 --rsa-key-txt（服务自己会报缺文件）
        bare = _fake_repo(base / "bare")
        cmd3, _ = ZmdLogsRunner(repo_dir=str(bare))._build_cmd()
        assert "--rsa-key-txt" not in cmd3


def test_gate_blocks_start() -> None:
    """闸门关闭时不得启动任何进程——这是权限边界，必须有测试守住。"""
    with tempfile.TemporaryDirectory() as td:
        repo = _fake_repo(Path(td) / "ZMDLogs", with_key=True)
        r = ZmdLogsRunner(repo_dir=str(repo), enabled=False)
        res = r.start()
        assert res["ok"] is False
        assert "zmdlogs_enabled" in res["error"]
        assert r.running() is False
        assert r._proc is None


def test_start_refused_when_not_ready() -> None:
    """前置不满足时不得拉起——否则会留下一个秒退的僵尸进程。"""
    with tempfile.TemporaryDirectory() as td:
        bare = _fake_repo(Path(td) / "bare")  # 无 RSA 私钥
        r = ZmdLogsRunner(repo_dir=str(bare), enabled=True)
        res = r.start()
        assert res["ok"] is False
        assert "前置未满足" in res["error"]
        assert r.running() is False
        assert r._proc is None, "启动被拒时不该留下 Popen 句柄"


def test_state_when_never_started() -> None:
    """首跑前调 state / stop 必须安全返回（不能 AttributeError）。"""
    with tempfile.TemporaryDirectory() as td:
        # 用一个确定没人监听的端口：state() 会探测端口，默认 29325 可能被
        # 用户手动开的 ZMDLogs 实例占着，那样 ws_open 就合法地为 True。
        r = ZmdLogsRunner(repo_dir=str(_fake_repo(Path(td) / "Z")), ws_port=29498)
        st = r.state()
        assert st["running"] is False and st["pid"] == 0
        assert st["ws_open"] is False
        assert st["external"] is False
        assert st["uptime_seconds"] == 0.0
        assert st["ws_url"].startswith("ws://127.0.0.1:")
        assert st["log_tail"] == []
        assert st["preflight"]["repo_found"] is True
        assert r.stop()["was_running"] is False


def test_state_is_json_serialisable() -> None:
    """state() 的结果要能直接进 JSON 响应（Path 对象会炸）。"""
    with tempfile.TemporaryDirectory() as td:
        r = ZmdLogsRunner(repo_dir=str(_fake_repo(Path(td) / "Z", with_key=True)), enabled=True)
        json.dumps(r.state(), ensure_ascii=False)


def test_ws_open_on_closed_port() -> None:
    """没进程时 ws_open 必须是 False 而不是抛异常。"""
    r = ZmdLogsRunner(ws_port=29399)
    assert r.ws_open() is False


def test_npcap_probe_returns_bool() -> None:
    assert isinstance(npcap_present(), bool)


def test_alive_reports_exit_code() -> None:
    """子进程秒退时 state 要报出退出码，而不是永远 running=False 无信息。"""
    with tempfile.TemporaryDirectory() as td:
        repo = _fake_repo(Path(td) / "Z", with_key=True)
        r = ZmdLogsRunner(repo_dir=str(repo), enabled=True)
        # 直接塞一个已结束的 Popen，模拟"拉起即退"
        import subprocess
        proc = subprocess.Popen(
            [sys.executable, "-c", "raise SystemExit(1)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        proc.wait()
        r._proc = proc
        st = r.state()
        assert st["running"] is False
        assert st["exit_code"] == 1, st
        r.stop()


# --------------------------------------------------------------------------- #
# exe 模式（绕过源码树缺失的 RSA 私钥 —— 预构建 EndfieldLogs.exe 自带密钥）
# --------------------------------------------------------------------------- #

def _fake_exe_dir(root: Path) -> Path:
    """造一个只含 EndfieldLogs.exe 的目录（模拟 Releases 解压版）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "EndfieldLogs.exe").write_bytes(b"MZ fake")
    return root


def test_find_exe() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        exe_dir = _fake_exe_dir(base / "EndfieldLogs")
        exe = exe_dir / "EndfieldLogs.exe"

        # 显式给 exe 文件
        assert find_exe(str(exe)) == exe
        # 显式给目录也应命中
        assert find_exe(str(exe_dir)) == exe
        # 名字不对不算
        (base / "other").mkdir()
        (base / "other" / "NotIt.exe").write_bytes(b"MZ")
        assert find_exe(str(base / "other" / "NotIt.exe")) is None
        # 空目录不算
        assert find_exe(str(base / "empty")) is None


def test_normalize_mode() -> None:
    assert normalize_mode("exe") == LAUNCH_MODE_EXE
    assert normalize_mode("source") == LAUNCH_MODE_SOURCE
    assert normalize_mode("EXE") == LAUNCH_MODE_EXE
    # 留空：配了 exe 路径按 exe，否则按 source（保持旧行为）
    assert normalize_mode("", "C:/x/EndfieldLogs.exe") == LAUNCH_MODE_EXE
    assert normalize_mode("", "") == LAUNCH_MODE_SOURCE
    assert normalize_mode("garbage") == LAUNCH_MODE_SOURCE


def test_exe_mode_preflight_needs_no_rsa() -> None:
    """exe 模式不该因缺 RSA 私钥而阻塞 —— 密钥编在预构建包里。"""
    with tempfile.TemporaryDirectory() as td:
        exe = _fake_exe_dir(Path(td) / "EndfieldLogs") / "EndfieldLogs.exe"
        r = ZmdLogsRunner(enabled=True, launch_mode="exe", exe_path=str(exe))
        pf = r.preflight()
        assert pf["launch_mode"] == LAUNCH_MODE_EXE
        assert pf["exe_found"] is True
        # 只要 Npcap 在，就该 ready（本机 Npcap 已装）
        if pf["npcap"]:
            assert pf["ready"] is True, pf["blocking"]
            assert pf["blocking"] == []


def test_exe_mode_missing_exe_blocks() -> None:
    with tempfile.TemporaryDirectory() as td:
        r = ZmdLogsRunner(enabled=True, launch_mode="exe",
                          exe_path=str(Path(td) / "nope" / "EndfieldLogs.exe"))
        pf = r.preflight()
        assert pf["exe_found"] is False
        assert pf["ready"] is False
        assert any("EndfieldLogs.exe" in b for b in pf["blocking"]), pf["blocking"]


def test_exe_mode_build_cmd() -> None:
    with tempfile.TemporaryDirectory() as td:
        exe = _fake_exe_dir(Path(td) / "EndfieldLogs") / "EndfieldLogs.exe"
        r = ZmdLogsRunner(enabled=True, launch_mode="exe",
                          exe_path=str(exe), ws_port=29402, log_level="DEBUG")
        cmd, cwd = r._build_cmd()
        # 预构建 exe 是纯 GUI 应用：不带任何参数，只认自己的 gui.json
        assert cmd == [str(exe)], cmd
        assert cwd == exe.parent, "cwd 要落在 exe 目录，才能按它的 _internal 定位资源"
        # 不该出现源码模式的任何参数
        assert "-m" not in cmd and "serve" not in cmd
        assert "--rsa-key-txt" not in cmd and "--ws-port" not in cmd


def test_exe_mode_reuses_running_instance() -> None:
    """exe 带单实例锁：端口已在监听时 start() 应复用而非重拉（重拉必退 -1）。"""
    with tempfile.TemporaryDirectory() as td:
        exe = _fake_exe_dir(Path(td) / "EndfieldLogs") / "EndfieldLogs.exe"
        r = ZmdLogsRunner(enabled=True, launch_mode="exe",
                          exe_path=str(exe), ws_port=29403)
        # 伪造"端口已开"：直接打桩 ws_open
        r.ws_open = lambda: True  # type: ignore[method-assign]
        res = r.start()
        assert res["ok"] is True
        assert res["already_running"] is True
        assert res.get("reused_external") is True
        assert r._proc is None, "复用外部实例时不该拉起新进程"


def test_source_mode_still_used_when_configured() -> None:
    """显式 source 模式仍走源码树（缺密钥时 preflight 如实阻塞）。"""
    with tempfile.TemporaryDirectory() as td:
        repo = _fake_repo(Path(td) / "ZMDLogs")  # 无密钥
        r = ZmdLogsRunner(repo_dir=str(repo), enabled=True, launch_mode="source")
        pf = r.preflight()
        assert pf["launch_mode"] == LAUNCH_MODE_SOURCE
        assert pf["ready"] is False
        assert any("RSA" in b for b in pf["blocking"]), pf["blocking"]


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  OK   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\nzmdlogs: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
