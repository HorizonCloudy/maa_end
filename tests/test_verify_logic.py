"""verify_game / 安装探测 / 推送 kwargs / llm_tool schema / effective 配置 的纯逻辑测试（不联网、不碰游戏）。

跑法：PYTHONPATH=<_host_sdk> python -m pytest -q tests/test_verify_logic.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 插件根的父目录：让 maa_end 包可导入

from maa_end.maaend import router as router_mod  # noqa: E402
from maa_end.maaend.infra import probe as detect  # noqa: E402
from plugin.sdk.plugin.llm_tool import collect_llm_tool_methods  # noqa: E402


def test_verify_enabled_modes() -> None:
    assert detect.verify_enabled("auto", {"found": True})
    assert not detect.verify_enabled("auto", {"found": False})
    assert detect.verify_enabled("on", {"found": False})
    assert not detect.verify_enabled("OFF", {"found": True})


def test_find_game_install_from_launcher_root(monkeypatch, tmp_path) -> None:
    exe = tmp_path / detect.GAME_EXE_REL
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(detect, "_launcher_roots", lambda: [str(tmp_path)])
    got = detect.find_game_install()
    assert got["found"] and detect.same_exe(got["exe"], str(exe))
    monkeypatch.setattr(detect, "_launcher_roots", lambda: [])
    monkeypatch.setattr(detect, "LAUNCHER_DIRS", (str(tmp_path / "nope"),))
    assert not detect.find_game_install()["found"]


def test_find_install_expands_downloads_glob(monkeypatch, tmp_path) -> None:
    (tmp_path / "MaaEnd-win-x86_64-v9").mkdir()
    (tmp_path / "MaaEnd-win.zip").write_bytes(b"")  # 压缩包不是目录，跳过
    monkeypatch.setattr(detect, "COMMON_DIRS", (str(tmp_path / "MaaEnd*"),))
    got = detect.find_install("")
    assert got["root"] == str(tmp_path / "MaaEnd-win-x86_64-v9")


class _Cfg:
    def __init__(self) -> None:
        self.stored = {"maa": {"assist_mode": "宏观建议", "confidence_threshold": 0.9}}

    async def update(self, patch, timeout=None):
        self.stored["maa"].update(patch["maa"])

    async def dump(self, timeout=None):
        return self.stored


class _Main:
    def __init__(self) -> None:
        self.config = _Cfg()
        self.pushed: list[dict] = []
        self.logger = None

    def push_message(self, **kwargs):  # 与 SDK 一致：只收关键字参数
        self.pushed.append(kwargs)
        return {"submitted": True}


def _router() -> router_mod.MaaEndRouter:
    r = router_mod.MaaEndRouter()
    r._bind(_Main())
    return r


def test_assist_push_uses_keyword_args() -> None:
    r = _router()
    r._assist_push("卡关了")
    (kw,) = r.main_plugin.pushed
    assert kw["parts"] == [{"type": "text", "text": "卡关了"}] and kw["ai_behavior"] == "read"


def test_assist_set_effective_keeps_stored_fields(monkeypatch) -> None:
    r = _router()
    monkeypatch.setattr(r, "_restart_assist", lambda: None)
    res = asyncio.run(r._assist_set({"assist_mode": "精细建议"}))
    eff = res.value["effective"]
    assert eff["assist_mode"] == "精细建议" and eff["confidence_threshold"] == 0.9


def test_llm_tools_with_args_declare_schema() -> None:
    metas = {m.name: m for m, _ in collect_llm_tool_methods(router_mod.MaaEndRouter())}
    required = {"maa_run_task": "name", "maa_ocr_find": "text", "maa_scene_expect": "classes",
                "maa_assist_advice": "events_json"}
    for name, field in required.items():
        assert metas[name].parameters["required"] == [field], name
    for name in ("maa_stream_read", "maa_ocr_read", "maa_scene", "maa_zmdlogs", "maa_daily"):
        assert metas[name].parameters["properties"], name
    assert metas["maa_daily"].timeout_seconds <= 300


def test_maa_daily_run_in_background_and_refuses_second(monkeypatch, tmp_path) -> None:
    import subprocess

    class _Proc:
        pid = 4242

        def poll(self):
            return None  # 仍在运行

        def wait(self, timeout=None):
            return 0

    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: calls.append(argv) or _Proc())
    r = _router()
    r.state.settings = {"allow_run": True, "maaend_dir": ""}
    monkeypatch.setattr(r, "data_path", lambda *p: tmp_path.joinpath(*p))
    out = asyncio.run(r.maa_daily_tool(action="run", minutes="30"))
    assert out["output"]["started"] and out["output"]["pid"] == 4242
    argv = calls[0]
    assert argv[1:3] == ["-X", "utf8"] and argv[3].endswith("daily_orchestrator.py")
    assert argv[argv.index("--minutes") + 1] == "30"
    for action in ("run", "reset"):
        assert asyncio.run(r.maa_daily_tool(action=action))["is_error"], action
    assert len(calls) == 1
