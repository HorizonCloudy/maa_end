"""SuiteClient 的 #3483 回退（maaend/p2p_http.py）：假宿主 /runs + /plugins，不碰真实宿主。

跑法：PYTHONPATH=<_host_sdk> python -m pytest -q tests/test_p2p_suite.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 插件根的父目录：让 maa_end 包可导入

from maa_end.maaend import suite  # noqa: E402
from plugin.sdk.plugin import Ok  # noqa: E402


class _Host:
    """mode: ok | fail | nofound（POST /runs 404 → 走 SDK）| hang（POST 已收到但不回）。"""

    def __init__(self, mode: str):
        self.mode, self.posts = mode, []
        host = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                host.posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                if host.mode == "hang":
                    return time.sleep(1.5)
                if host.mode == "nofound":
                    return self._send(404, {"detail": "Not Found"})
                self._send(200, {"run_id": "r1", "status": "queued", "run_token": "tok"})

            def do_GET(self):
                if self.path == "/plugins":
                    return self._send(200, {"plugins": [{"id": "game_decision", "runtime_enabled": True},
                                                        {"id": "off_layer", "runtime_enabled": False}]})
                if self.path == "/runs/r1":
                    if host.mode == "fail":
                        return self._send(200, {"status": "failed",
                                                "error": {"code": "PLUGIN_ERROR", "message": "boom"}})
                    return self._send(200, {"status": "succeeded"})
                if self.path.startswith("/runs/r1/export"):
                    data = {"agent": {"running": True, "target_process": "endfield.exe"}}
                    return self._send(200, {"items": [{"label": "trigger_response",
                                                       "json": {"success": True, "data": data}}]})
                self._send(404, {})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


class _SdkPlugins:
    """SDK 访问器桩：记录是否被回退调用。"""

    def __init__(self):
        self.used = []

    async def call_entry_json(self, ref, args, timeout=None):
        self.used.append(ref)
        return Ok({"via": "sdk"})

    async def require_enabled(self, plugin_id, timeout=None):
        self.used.append(plugin_id)
        return Ok({"plugin_id": plugin_id})


@pytest.fixture
def host(monkeypatch, request):
    h = _Host(request.param)
    monkeypatch.setenv("NEKO_PLUGIN_HOST_URL", h.url)
    yield h
    h.srv.shutdown()


@pytest.mark.parametrize("host", ["ok"], indirect=True)
def test_call_goes_over_http(host) -> None:
    sdk = _SdkPlugins()
    c = suite.SuiteClient(sdk)
    st = c.nitrogen_playing(asyncio.run(c.nitrogen_status()))
    assert st == {"known": True, "running": True, "target_process": "endfield.exe"}, st
    assert host.posts == [{"plugin_id": "nitrogen_agent", "entry_id": "get_agent_status", "args": {}}]
    assert sdk.used == []
    assert asyncio.run(c.layer_enabled()) is True
    assert asyncio.run(suite.SuiteClient(sdk, layer_id="off_layer").layer_enabled()) is False
    assert asyncio.run(suite.SuiteClient(sdk, layer_id="missing").layer_enabled()) is False
    assert sdk.used == []


@pytest.mark.parametrize("host", ["fail"], indirect=True)
def test_entry_failure_is_error_dict(host) -> None:
    out = asyncio.run(suite.SuiteClient(_SdkPlugins()).route("x"))
    assert out == {"error": "boom", "plugin": "game_decision", "entry": "gd_route"}, out


@pytest.mark.parametrize("host", ["nofound"], indirect=True)
def test_no_runs_api_falls_back_to_sdk(host) -> None:
    sdk = _SdkPlugins()
    assert asyncio.run(suite.SuiteClient(sdk).layer_status()) == {"via": "sdk"}
    assert sdk.used == ["game_decision:gd_status"]


def test_unreachable_host_falls_back_to_sdk(monkeypatch) -> None:
    monkeypatch.setenv("NEKO_PLUGIN_HOST_URL", "http://127.0.0.1:0")
    sdk = _SdkPlugins()
    c = suite.SuiteClient(sdk)
    assert asyncio.run(c.layer_enabled()) is True
    assert asyncio.run(c.layer_status()) == {"via": "sdk"}
    assert sdk.used == ["game_decision", "game_decision:gd_status"]


@pytest.mark.parametrize("host", ["hang"], indirect=True)
def test_post_sent_then_timeout_does_not_double_execute(host) -> None:
    sdk = _SdkPlugins()
    out = asyncio.run(suite.SuiteClient(sdk).call("nitrogen_agent", "start_game_agent", timeout=1.0))
    assert "error" in out, out
    assert len(host.posts) == 1 and sdk.used == [], (host.posts, sdk.used)
