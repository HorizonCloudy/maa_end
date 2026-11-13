"""Runtime-safe cross-plugin call: host HTTP Run API first, SDK call_entry as fallback.

shortcut: works around Project-N-E-K-O/N.E.K.O#3483 -- ``self.plugins.call_entry`` /
``call_entry_json`` awaited from an entry or timer silently times out (the response
asyncio.Queue is bound to the startup loop). Delete this module and go back to
``self.plugins.call_entry*`` once upstream fixes #3483.

Usage (one-line swap)::

    res = await call_entry_fallback(self.plugins, "game_kb:status", {}, timeout=8, json_only=True)

Returns the same ``Ok(value)`` / ``Err(SdkError)`` the SDK returns. ``value`` is the
entry's return value, read from the run export item ``trigger_response`` -> ``json.data``.
Known gap: an entry that returns a legacy ``ok()`` envelope dict gets the full envelope
from the SDK but only its ``data`` here (the host normalizes everything to an envelope).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from plugin.sdk.plugin import Err, Ok, SdkError

_TERMINAL = ("succeeded", "failed", "canceled", "timeout")  # plugin/runs/manager.py _TERMINAL_STATUSES
# Bypass HTTP(S)_PROXY / system proxy: the host is always loopback.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _Unreachable(Exception):
    """HTTP path unusable before a run exists -> use the SDK instead."""


def host_base_url() -> str:
    """NEKO_PLUGIN_HOST_URL > launcher-published port (inherited by plugin processes) > 48916."""
    url = os.getenv("NEKO_PLUGIN_HOST_URL", "").strip()
    if url:
        # Loopback only: entry args and the per-run Bearer token go to this host. A set but
        # non-loopback value disables the HTTP path (port 0 never connects -> SDK fallback)
        # instead of silently falling through to the live host below.
        if urllib.parse.urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1"):
            return url.rstrip("/")
        return "http://127.0.0.1:0"
    # Same keys as config/network.py (_read_port_env / resolve_user_plugin_base).
    for key in ("NEKO_USER_PLUGIN_SERVER_PORT", "USER_PLUGIN_SERVER_PORT"):
        raw = os.getenv(key, "").strip()
        if raw.isdigit() and 0 < int(raw) <= 65535:
            return f"http://127.0.0.1:{int(raw)}"
    return "http://127.0.0.1:48916"


def _http(method: str, url: str, payload: object = None, token: str = "", timeout: float = 5.0) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _OPENER.open(req, timeout=max(timeout, 0.05)) as resp:
        return json.loads(resp.read() or b"{}")


def _err(message: str, code: str, ref: str) -> Err:
    return Err(SdkError(message, code=code, op_name="p2p_http.call_entry_fallback", entry_ref=ref))


async def _call_http(base: str, ref: str, plugin_id: str, entry_id: str, args: dict, deadline: float):
    try:
        # Half the budget at most, so the SDK fallback still has time if POST hangs.
        created = await asyncio.to_thread(
            _http, "POST", f"{base}/runs",
            {"plugin_id": plugin_id, "entry_id": entry_id, "args": args},
            "", (deadline - time.monotonic()) / 2,
        )
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            raise _Unreachable(f"HTTP {e.code}") from e
        body = e.read().decode("utf-8", "replace")[:300]
        return _err(f"POST /runs HTTP {e.code}: {body}", "HTTP_ERROR", ref)
    except urllib.error.URLError as e:  # urllib wraps connect errors/timeouts: request never sent
        raise _Unreachable(repr(e)) from e
    except (ConnectionError, socket.timeout, TimeoutError, ValueError) as e:  # run may exist: no fallback (no double-execute)
        code = "TIMEOUT" if isinstance(e, TimeoutError) else "TRANSPORT"  # socket.timeout is TimeoutError (3.10+)
        return _err(f"POST /runs failed after sending: {e!r}", code, ref)

    run_id, token = created.get("run_id"), created.get("run_token", "")
    if not run_id:
        raise _Unreachable(f"no run_id in {created!r}")
    delay = 0.1
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return _err(f"entry {ref!r} timed out (run {run_id})", "TIMEOUT", ref)
        try:
            rec = await asyncio.to_thread(_http, "GET", f"{base}/runs/{run_id}", None, token, left)
        except (urllib.error.URLError, OSError, ValueError) as e:  # run exists: no fallback (no double-execute)
            return _err(f"polling run {run_id} failed: {e!r}", "TRANSPORT", ref)
        status = rec.get("status")
        if status in _TERMINAL:
            break
        await asyncio.sleep(min(delay, max(left, 0)))
        delay = min(delay * 1.5, 0.5)

    if status != "succeeded":
        error = rec.get("error") or {}
        msg = error.get("message") if isinstance(error, dict) else str(error)
        code = error.get("code") if isinstance(error, dict) else None
        return _err(msg or f"run {status}", code or status.upper(), ref)
    try:
        export = await asyncio.to_thread(
            _http, "GET", f"{base}/runs/{run_id}/export", None, token,
            max(deadline - time.monotonic(), 1.0),  # run already done; don't drop its result on a tight budget
        )
    except (urllib.error.URLError, OSError, ValueError) as e:
        return _err(f"reading export of run {run_id} failed: {e!r}", "TRANSPORT", ref)
    for item in export.get("items") or []:
        if item.get("label") == "trigger_response" and isinstance(item.get("json"), dict):
            return Ok(item["json"].get("data"))
    return _err(f"run {run_id} has no trigger_response export", "NO_RESULT", ref)


async def call_entry_fallback(plugins, ref: str, args: dict | None = None, *,
                              timeout: float = 10.0, json_only: bool = False):
    """Drop-in for ``plugins.call_entry`` (``json_only=True``: ``call_entry_json``). See module doc."""
    deadline = time.monotonic() + timeout
    plugin_id, sep, entry_id = ref.partition(":")
    if not (sep and plugin_id and entry_id):
        return _err(f"invalid entry ref {ref!r}, expected 'plugin_id:entry_id'", "INVALID_ARGUMENT", ref)
    try:
        res = await _call_http(host_base_url(), ref, plugin_id, entry_id, dict(args or {}), deadline)
    except _Unreachable:
        if plugins is None:
            return _err("host HTTP API unreachable and no plugins accessor", "UNAVAILABLE", ref)
        call = plugins.call_entry_json if json_only else plugins.call_entry
        return await call(ref, args or {}, timeout=max(deadline - time.monotonic(), 0.1))
    # Same rule as SDK call_entry_json: None or dict only.
    if json_only and isinstance(res, Ok) and not (res.value is None or isinstance(res.value, dict)):
        return _err(f"entry {ref!r} returned non-object payload: {type(res.value)!r}", "INVALID_PAYLOAD", ref)
    return res


async def require_enabled_fallback(plugins, plugin_id: str, *, timeout: float = 5.0):
    """Drop-in for ``plugins.require_enabled`` (same #3483 bug: it goes through ctx.query_plugins).

    ``GET /plugins`` -> ``Ok(item)`` when listed and not runtime-disabled. GET is read-only, so any
    HTTP failure falls back to the SDK (half the budget each, like call_entry_fallback).
    """
    deadline = time.monotonic() + timeout
    try:
        listed = await asyncio.to_thread(_http, "GET", f"{host_base_url()}/plugins", None, "", timeout / 2)
    except (urllib.error.URLError, OSError, ValueError):
        if plugins is None:
            return _err("host HTTP API unreachable and no plugins accessor", "UNAVAILABLE", plugin_id)
        return await plugins.require_enabled(plugin_id, timeout=max(deadline - time.monotonic(), 0.1))
    for item in listed.get("plugins") or []:
        if isinstance(item, dict) and item.get("id") == plugin_id:
            if item.get("runtime_enabled") is False:
                return _err(f"required plugin is disabled: {plugin_id!r}", "PLUGIN_DISABLED", plugin_id)
            return Ok(item)
    return _err(f"required plugin not found: {plugin_id!r}", "PLUGIN_NOT_FOUND", plugin_id)
