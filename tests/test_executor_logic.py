#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""executor/controlapi 纯逻辑自检：策略矩阵 / 失败跟踪 / M3 进入退出 / HTTP 客户端。

跑法：python tests/test_executor_logic.py   （零依赖，不用 SDK）
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "maaend"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import executor  # noqa: E402
from backends.control import ControlClient, ControlError  # noqa: E402


def test_choose_mode() -> None:
    # auto 链：闸门 → M1 → M2 → M0；M3/M4 永不出现在结果里
    assert executor.choose_mode(allow_run=False) == ("m0", "闸门未放行（maa.allow_run=false）→ 只读模式")
    m1 = executor.choose_mode(m1_ok=True, m2_ok=True)
    assert m1[0] == "m1" and "在线" in m1[1]
    assert executor.choose_mode(m1_ok=False, m2_ok=True)[0] == "m2"
    dead = executor.choose_mode(m1_ok=False, m2_ok=False)
    assert dead[0] == "m0" and "不可用" in dead[1]
    for kw in ({"m1_ok": True}, {"m2_ok": True}, {"allow_run": False}, {}):
        assert executor.choose_mode(**kw)[0] not in ("m3", "m4")


def test_fail_tracker() -> None:
    ft = executor.FailTracker(threshold=2)
    ft.observe(running=True, failed=True, succeeded=False)  # 运行中不计
    assert ft.streak == 0
    ft.observe(running=False, failed=True, succeeded=False)
    assert ft.take_suggestion() is None  # 第一次失败：未到阈值
    ft.observe(running=False, failed=True, succeeded=False)
    hint = ft.take_suggestion()
    assert hint and "连续失败 2 次" in hint
    assert ft.take_suggestion() is None  # 同轮只建议一次
    ft.observe(running=False, failed=False, succeeded=True)
    assert ft.streak == 0 and ft.suggested is False


class FakeSuite:
    """假 SuiteClient：记录调用，可注入失败。"""

    def __init__(self, fail_at=()):
        self.calls = []
        self.fail_at = set(fail_at)

    async def call_like(self, ref, args):
        self.calls.append((ref, args))
        if ref in self.fail_at:
            return {"error": f"{ref} down", "plugin": "nitrogen_agent", "entry": ref}
        return {"ok": True, "ref": ref, "args": args}

    async def set_suite_mode(self, mode, confirm=False):
        return await self.call_like("set_suite_mode", {"mode": mode, "confirm": confirm})

    async def start_agent(self, process, force=False):
        return await self.call_like("start_game_agent", {"process": process})


async def test_nitrogen_flow() -> None:
    suite = FakeSuite()
    out = await executor.enter_nitrogen(suite, "Endfield.exe")
    assert not out.get("error") and out.get("ok")
    assert suite.calls[0][0] == "set_suite_mode"
    assert suite.calls[0][1] == {"mode": "nitrogen", "confirm": True}  # confirm 必带
    assert suite.calls[1][0] == "start_game_agent"

    # start 失败 → 自动还原（补一个 decision 切换）
    bad = FakeSuite(fail_at={"start_game_agent"})
    out2 = await executor.enter_nitrogen(bad, "Endfield.exe")
    assert out2.get("error") and "start_game_agent" in str(out2)
    modes = [c for c in bad.calls if c[0] == "set_suite_mode"]
    assert modes[-1][1]["mode"] == "decision"  # 还原调用发生

    # set 失败 → 直接带 error 返回，不启代理
    worse = FakeSuite(fail_at={"set_suite_mode"})
    out3 = await executor.enter_nitrogen(worse, "Endfield.exe")
    assert out3.get("error") and out3.get("step") == "set_suite_mode"
    assert not any(c[0] == "start_game_agent" for c in worse.calls)


def test_control_client_offline() -> None:
    client = ControlClient("http://127.0.0.1:1", install_root="", spawn=False)
    assert client.health(timeout=0.2) is None  # 不可达 → None 不抛
    assert client.exe_path() == ""  # 无安装根
    try:
        client.run("x", {})
        raise AssertionError("应当抛 ControlError")
    except ControlError:
        pass


def test_control_client_request_shapes() -> None:
    """用假 urlopen 验证：URL 拼接 / POST body / HTTPError 的 error 透传。"""
    import urllib.error as ue

    client = ControlClient("http://127.0.0.1:8829/", install_root="", spawn=False)
    assert client.base == "http://127.0.0.1:8829"

    captured = {}

    class FakeResp:
        def __init__(self, body):
            self._body = json.dumps(body).encode("utf-8")

        def read(self, *_):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["data"] = req.data
        if "boom" in req.full_url:
            err = ue.HTTPError(req.full_url, 409, "Conflict", {}, None)
            err.read = lambda *a: json.dumps({"ok": False, "error": "窗口未找到"}).encode("utf-8")
            raise err
        return FakeResp({"ok": True, "value": 1})

    import backends.control as controlapi

    orig = controlapi._rq.urlopen
    controlapi._rq.urlopen = fake_urlopen
    try:
        out = client.run("SellProduct", {"某选项": "Daily"})
        assert out == {"ok": True, "value": 1}
        assert captured["url"] == "http://127.0.0.1:8829/run"
        assert captured["method"] == "POST"
        body = json.loads(captured["data"].decode("utf-8"))
        assert body == {"task": "SellProduct", "options": {"某选项": "Daily"}}

        client.events(42)  # 只为驱动请求，返回值由下面的 captured 断言覆盖
        assert captured["url"] == "http://127.0.0.1:8829/events?format=json&since=42"
        assert captured["method"] == "GET"

        try:
            client._request("/boom")
            raise AssertionError("应当抛 ControlError")
        except ControlError as exc:
            assert "窗口未找到" in str(exc)  # 服务端中文原因透传
    finally:
        controlapi._rq.urlopen = orig


def test_stream_control_poll() -> None:
    """StreamService 的 M1 事件轮询：首拉对齐 latest，增量入流带 source=m1。"""
    from maaend import stream as stream_mod

    class FakeControl:
        def __init__(self):
            self.polled = []
            self.buf = [
                {"id": 1, "ts": 1.0, "kind": "task_starting", "data": {"entry": "A"}},
                {"id": 2, "ts": 2.0, "kind": "task_succeeded", "data": {"entry": "A"}},
            ]
            self.latest = 2

        def events(self, since):
            self.polled.append(since)
            return {"ok": True, "entries": [e for e in self.buf if e["id"] > since],
                    "latest_id": self.latest}

    svc = stream_mod.StreamService(
        log_path=Path("Z:/nonexistent/maafw.log"),
        window_probe=lambda: {"found": False, "hwnd": 0, "title": "", "class": ""},
        detector=None,
        settings={"window_interval_seconds": 3600},
        control=FakeControl(),
    )
    svc._drain_control()  # 首拉：对齐 latest，不重放
    assert svc._control_cursor == 2
    assert svc.store.read(category="tasks") == []
    ctrl = svc._control
    ctrl.buf.append({"id": 3, "ts": 3.0, "kind": "node", "data": {"node": "N1"}})
    ctrl.latest = 3
    svc._drain_control()  # 增量
    entries = svc.store.read(category="tasks", since=0, limit=10)
    assert len(entries) == 1
    assert entries[0]["kind"] == "node"
    assert entries[0]["data"] == {"node": "N1", "source": "m1"}
    assert svc._control_cursor == 3


def test_write_config_adb() -> None:
    """runner.write_config 的 ADB 块（三键必填）与不带 adb 的形态。"""
    import json as _json
    import sys as _sys
    import tempfile as _tf
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
    from maaend.backends.pi_runner import PiRunner

    with _tf.TemporaryDirectory() as d:
        r = PiRunner(_P(d), d)
        r.write_config(
            controller="ADB", resource="官服",
            window_class="", window_title="",
            tasks=[{"name": "T", "option": []}],
            adb={"name": "127.0.0.1:5555-adb", "adb_path": "adb", "address": "127.0.0.1:5555"})
        cfg = _json.loads((_P(d) / "config" / "maa_pi_config.json").read_text(encoding="utf-8"))
        assert cfg["controller"] == {"name": "ADB"}
        assert cfg["adb"] == {"name": "127.0.0.1:5555-adb", "adb_path": "adb",
                              "address": "127.0.0.1:5555"}
        assert cfg["task"] == [{"name": "T", "option": []}]


def test_control_run_body_shapes() -> None:
    """controlapi.run 带/不带 controller 的请求体形状。"""
    import backends.control as _cap

    captured = {}

    class FakeResp:
        def read(self, *_):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        return FakeResp()

    orig = _cap._rq.urlopen
    _cap._rq.urlopen = fake_urlopen
    try:
        c = _cap.ControlClient("http://127.0.0.1:8829", install_root="", spawn=False)
        c.run("T")
        assert _json_loads(captured["data"]) == {"task": "T", "options": {}}
        c.run("T", {"o": "c"}, controller="ADB", adb_serial="127.0.0.1:5555")
        body = _json_loads(captured["data"])
        assert body == {"task": "T", "options": {"o": "c"},
                        "controller": "ADB", "adb_serial": "127.0.0.1:5555"}
    finally:
        _cap._rq.urlopen = orig


def _json_loads(b):
    import json as _j
    return _j.loads(b.decode("utf-8"))


def test_controller_type_of() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "maaend"))
    from backends.catalog import controller_type_of
    cat = {"controller": [
        {"name": "Win32-Front", "type": "Win32"},
        {"name": "ADB", "type": "Adb"},
        {"name": "CloudADB", "type": "Adb"},
    ]}
    assert controller_type_of(cat, "ADB") == "Adb"
    assert controller_type_of(cat, "Win32-Front") == "Win32"
    assert controller_type_of(cat, "nope") == ""
    assert controller_type_of({}, "ADB") == ""


def test_probe_modes_m5() -> None:
    """M5 探测注入：可用透传 / 缺席如实不可用 / 探测异常降级不抛出（七模式 = M0-M6）。"""
    class _Suite:
        async def layer_enabled(self):
            return False

    async def _m5_ok():
        return {"available": True, "detail": {"mode": "idle", "running": False}}

    async def _m5_boom():
        raise RuntimeError("probe down")

    async def _run() -> None:
        base = dict(settings={"allow_run": True}, control_health=lambda: None,
                    install_probe=lambda: {"found": False}, suite=_Suite())
        p1 = await executor.probe_modes(**base, m5_probe=_m5_ok)
        m5 = {m["mode"]: m for m in p1["modes"]}
        assert len(p1["modes"]) == 7 and m5["m5"]["available"] is True
        assert m5["m5"]["detail"]["mode"] == "idle"

        p2 = await executor.probe_modes(**base)  # 不传 probe → 如实不可用
        m5b = {m["mode"]: m for m in p2["modes"]}
        assert m5b["m5"]["available"] is False

        p3 = await executor.probe_modes(**base, m5_probe=_m5_boom)
        m5c = {m["mode"]: m for m in p3["modes"]}
        assert m5c["m5"]["available"] is False
        assert "探测失败" in str(m5c["m5"].get("reason", ""))

    asyncio.run(_run())


def test_prepare_follows_install_upgrade() -> None:
    """换 MaaEnd 版本后 prepare 必须刷新二进制并把 junction 改指新目录（旧目录已删也行）。"""
    import os as _os
    import shutil as _sh
    import sys as _sys
    import tempfile as _tf
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
    from maaend.backends.pi_runner import PiRunner

    def fake_install(root: _P, ver: str) -> None:
        (root / "maafw" / "plugins").mkdir(parents=True)
        (root / "maafw" / "MaaPiCli.exe").write_text("exe " + ver)
        (root / "maafw" / "MaaFramework.dll").write_text("dll " + ver)
        (root / "maafw" / "plugins" / "p.dll").write_text("plugin " + ver)
        (root / "resource").mkdir()
        (root / "resource" / "v.txt").write_text(ver)
        (root / "interface.json").write_text("{}")
        (root / "agent").mkdir()
        (root / "agent" / "go-service.exe").write_text("agent " + ver)

    with _tf.TemporaryDirectory() as d:
        old, new, run = _P(d) / "v1", _P(d) / "v2", _P(d) / "runner"
        fake_install(old, "v1")
        fake_install(new, "v2.35")
        orig = PiRunner._merged_interface
        PiRunner._merged_interface = staticmethod(
            lambda _p: {"agent": [{"child_exec": "agent/go-service"}]}
        )
        try:
            PiRunner(run, str(old)).prepare()
            # 首跑(agent/ junction 刚建)也要补上 .exe
            ce = json.loads((run / "interface.json").read_text(encoding="utf-8"))["agent"][0]["child_exec"]
            assert ce.endswith("go-service.exe"), ce
            _sh.rmtree(old)  # 用户删掉旧版本目录 → 旧 junction 悬空
            PiRunner(run, str(new)).prepare()
        finally:
            PiRunner._merged_interface = orig
        assert (run / "MaaPiCli.exe").read_text() == "exe v2.35"
        assert (run / "MaaFramework.dll").read_text() == "dll v2.35"
        assert (run / "plugins" / "p.dll").read_text() == "plugin v2.35"
        assert (run / "resource" / "v.txt").read_text() == "v2.35"
        assert _os.path.realpath(run / "resource").lower() == _os.path.realpath(new / "resource").lower()


def main() -> None:
    test_choose_mode()
    test_probe_modes_m5()
    test_fail_tracker()
    test_write_config_adb()
    test_control_run_body_shapes()
    test_controller_type_of()
    asyncio.run(test_nitrogen_flow())
    test_control_client_offline()
    test_control_client_request_shapes()
    test_stream_control_poll()
    test_prepare_follows_install_upgrade()
    print("test_executor_logic: ALL PASS")


if __name__ == "__main__":
    main()
