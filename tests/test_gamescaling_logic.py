#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gamescaling（M6）纯逻辑自检：动作解析 / 键位映射 / 提示词 / 计划差分 / 请求体 / 代理闸门。

跑法：python tests/test_gamescaling_logic.py   （零依赖，不用 SDK，不碰网络——
唯一的回环连接打向必拒端口，与 conftest 的隔离策略一致）
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "maaend"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import executor  # noqa: E402
from backends.agent import AgentError, GamescalingAgent, policy_config_from_settings  # noqa: E402
from backends.gamescaling import PolicyConfig, parse_action  # noqa: E402
from backends.gamescaling.action import (  # noqa: E402
    ACTION_START,
    KEY_VOCAB,
    MOUSE_BUTTON_KEYS,
    NOOP_ACTION_TEXT,
    Action,
)
from backends.gamescaling.keymaps import TO_WIN32_VK  # noqa: E402
from backends.gamescaling.policy import (  # noqa: E402
    GameScalingPolicy,
    Turn,
    assistant_turn,
    build_request,
    encode_bgr,
    finalize,
    user_turn,
)
from backends.gamescaling.prompt import MOUSE_AIM_LINE, build_system_prompt  # noqa: E402
from backends.input_io import WHEEL_DELTA, plan_chunk  # noqa: E402

_DATA = Path(__file__).resolve().parent.parent / "data" / "gamescaling"


# ---------------------------------------------------------------- 动作解析

def test_parse_tagged_after_think() -> None:
    # 上游 README 的 Doom 实例回复（空思考 + 转向 + 全程开火）
    text = "<think>\n\n</think>\n\n<|action_start|>-37 -3 0 ; LB S ; LB ; LB ; LB ; LB ; LB<|action_end|>"
    a = parse_action(text)
    assert a.parsed and not a.truncated
    assert a.mouse == (-37.0, -3.0, 0.0)
    assert a.steps[0] == ["LB", "S"]
    assert a.steps[1:] == [["LB"]] * 5
    assert a.body == "-37 -3 0 ; LB S ; LB ; LB ; LB ; LB ; LB"


def test_parse_bare_body() -> None:
    # 特殊 token 被服务端剥掉时返回裸 body
    a = parse_action("0 0 0 ; W ; W ; W ; ; ; ")
    assert a.parsed
    assert a.mouse == (0.0, 0.0, 0.0)
    assert a.steps == [["W"], ["W"], ["W"], [], [], []]


def test_parse_reasoning_quoting_format() -> None:
    # 思考型模型会在 reasoning 里引用格式本身；动作只从最后一个 </think> 之后读
    quoted = f"the format is {ACTION_START}X Y Z ; ... inside thinking"
    text = (f"<think>{quoted}</think>"
            f"<|action_start|>10 20 -2 ; W ; ; ; ; ; <|action_end|>")
    a = parse_action(text)
    assert a.parsed
    assert a.mouse == (10.0, 20.0, -2.0)
    assert a.steps[0] == ["W"]


def test_parse_truncated_think_is_noop() -> None:
    a = parse_action("<think>还在思考就被 max_tokens 截断")
    assert not a.parsed and a.truncated
    assert a.is_noop
    assert a.history_text == NOOP_ACTION_TEXT  # 失败轮存显式 no-op，不教模型重复失败


def test_parse_clamps_and_drops() -> None:
    a = parse_action("<|action_start|>1500 -2000 9 ; W xx Q ; ; ; ; ; <|action_end|>")
    assert a.parsed
    assert a.mouse == (1000.0, -1000.0, 5.0)  # 鼠标 ±1000、滚轮 ±5
    assert a.steps[0] == ["W", "Q"]  # 词表外的 xx 被丢弃
    assert a.dropped == ["xx"]


def test_parse_lenient_recovers() -> None:
    # 动作被卡在未闭合 <think> 里：严格模式 no-op，宽松模式恢复
    text = "<think>maybe I should<|action_start|>5 5 0 ; W ; W ; ; ; ; <|action_end|>"
    assert not parse_action(text).parsed
    a = parse_action(text, lenient=True)
    assert a.parsed and a.mouse == (5.0, 5.0, 0.0)


def test_action_max_keys_per_step() -> None:
    # 词表内但超过 4 个：_build 截前 4 个（按出现序），Q 不进 dropped（上游口径）
    a = parse_action("<|action_start|>0 0 0 ; W A S D Q ; ; ; ; ; <|action_end|>")
    assert a.steps[0] == ["W", "A", "S", "D"]
    assert plan_chunk(a, 100, 100)[0].press == ("A", "D", "S", "W")  # 计划层按字典序


# ---------------------------------------------------------------- 键位映射

def test_win32_vk_covers_keyboard_vocab() -> None:
    keyboard = KEY_VOCAB - set(MOUSE_BUTTON_KEYS)
    assert set(TO_WIN32_VK) == keyboard  # 恰好覆盖：不缺也不多
    assert TO_WIN32_VK["A"] == 0x41 and TO_WIN32_VK["Z"] == 0x5A
    assert TO_WIN32_VK["zero"] == 0x30 and TO_WIN32_VK["one"] == 0x31
    assert TO_WIN32_VK["nine"] == 0x39
    assert TO_WIN32_VK["One"] == 0x70 and TO_WIN32_VK["Twelve"] == 0x7B
    assert TO_WIN32_VK["Esc"] == 0x1B and TO_WIN32_VK["Space"] == 0x20
    assert TO_WIN32_VK["Shift"] == 0x10 and TO_WIN32_VK["Ctrl"] == 0x11


# ---------------------------------------------------------------- 提示词

def test_build_system_prompt_sections() -> None:
    p = build_system_prompt("TestGame", rules="R1\nR2",
                            key_bindings={"W": "Move", "LMB": "Fire"},
                            mouse_aim=True)
    # 分节头是训练见过的字面文本，逐字校验
    for marker in ("**Game Rules**", "**Common Keys in TestGame**",
                   "**Output Format**", "**Explanation**", "**Key Naming Rules**",
                   "**Action only (usual)**", "**Thinking + action (when necessary)**"):
        assert marker in p, marker
    assert "- W -> Move" in p
    assert "- LB -> Fire" in p  # LMB 别名归一到 LB
    assert MOUSE_AIM_LINE in p  # 鼠标行逐字（上游要求 verbatim）
    no_aim = build_system_prompt("G", mouse_aim=False)
    assert MOUSE_AIM_LINE not in no_aim


def test_shipped_prompt_byte_exact() -> None:
    # 随插件分发的终末地提示词 = 模板生成（build_prompt.py）逐字节一致，
    # 防手滑改坏训练格式段
    spec = importlib.util.spec_from_file_location(
        "build_prompt", _DATA / "build_prompt.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    expected = build_system_prompt(mod.GAME_NAME, rules=mod.RULES,
                                   key_bindings=mod.KEYS, mouse_aim=True)
    shipped = (_DATA / "endfield_system_prompt.txt").read_text(encoding="utf-8")
    assert shipped == expected + "\n"


# ---------------------------------------------------------------- 计划差分（gamescaling_io 纯函数）

def test_plan_chunk_step_diffs() -> None:
    a = parse_action("<|action_start|>0 0 0 ; W ; W ; W LB ; LB ; ; <|action_end|>")
    plans = plan_chunk(a, 1000, 1000)
    assert len(plans) == 6
    assert plans[0].press == ("W",) and not plans[0].release
    assert plans[1].press == () and plans[1].release == ()  # 连续组不重按
    assert plans[2].press == ("LB",)  # W 继续按住
    assert plans[3].release == ("W",) and plans[3].press == ()
    assert plans[4].release == ("LB",)  # 空组释放全部
    assert plans[5] == plans[5]  # 全空步


def test_plan_chunk_mouse_spread_sums_exact() -> None:
    a = parse_action("<|action_start|>333 -250 2 ; ; ; ; ; ; <|action_end|>")
    plans = plan_chunk(a, 1280, 720)
    assert sum(p.dx for p in plans) == round(333 * 1280 / 1000)  # 426，误差扩散总和精确
    assert sum(p.dy for p in plans) == round(-250 * 720 / 1000)  # -180
    assert plans[0].wheel == 2 * WHEEL_DELTA  # Z>0 向上，Windows 每格 120
    assert all(p.wheel == 0 for p in plans[1:])
    neg = plan_chunk(parse_action("<|action_start|>0 0 -1 ; ; ; ; ; ; <|action_end|>"), 10, 10)
    assert neg[0].wheel == -WHEEL_DELTA


def test_plan_chunk_unparsed_is_noop() -> None:
    plans = plan_chunk(Action(), 100, 100)  # parsed=False
    assert len(plans) == 6
    assert all(not p.press and not p.release and not p.dx and not p.dy and not p.wheel
               for p in plans)


# ---------------------------------------------------------------- 请求体与编码（policy 离线部分）

def _cfg() -> PolicyConfig:
    return PolicyConfig()


def test_build_request_body_contract() -> None:
    msgs = [{"role": "system", "content": "SYS"}, user_turn("data:image/png;base64,AA==")]
    body = build_request(msgs, _cfg())
    assert body["model"] == "gamescaling" and body["temperature"] == 1.0
    assert body["skip_special_tokens"] is False  # 动作 tag 是特殊 token，必须保留
    assert body["stop"] == ["<|action_end|>"]
    assert body["continue_final_message"] is True   # 续写预填的 assistant 轮
    assert body["add_generation_prompt"] is False
    assert body["messages"][-1] == {"role": "assistant", "content": "<think>"}
    nr = build_request([], PolicyConfig(reasoning=False))
    assert nr["messages"][-1]["content"] == "<think>\n\n</think>"  # 无思考模式的预填


def test_finalize_restores_prefill_and_stop() -> None:
    # vLLM 不回传预填、stop 串也被剥掉——finalize 补回，让回复按训练格式解析。
    # 无思考模式（预填已闭合空 think）下裸 body 能被完整还原。
    text = finalize("0 0 0 ; ; ; ; ; ; ", "stop", PolicyConfig(reasoning=False))
    a = parse_action(text)
    assert a.parsed and a.is_noop
    # 思考模式下模型直接出动作（think 未闭合）：如实判截断 no-op，不猜
    truncated = finalize("0 0 0 ; ; ; ; ; ; ", "stop", _cfg())
    assert not parse_action(truncated).parsed


def test_assistant_turn_history_rendering() -> None:
    assert assistant_turn("<|action_start|>A<|action_end|>")["content"] == "<|action_start|>A<|action_end|>"
    with_think = assistant_turn("<|action_start|>A<|action_end|>", "why")
    assert with_think["content"].startswith("<think>\nwhy\n</think>")


def test_messages_structure_and_trim() -> None:
    p = GameScalingPolicy("SYS", PolicyConfig(history_len=2))
    for i, act in enumerate(("A", "B", "C")):
        p._history.append(Turn(f"url{i}", f"r{i}", f"<|action_start|>{act}<|action_end|>"))
        while len(p._history) > p.cfg.history_len:
            p._history.popleft()  # act_bgr 的裁剪口径
    msgs = p.messages("url_now")
    assert msgs[0] == {"role": "system", "content": "SYS"}
    # 保留最近 2 轮：B、C（history_think=0 → 思考文本丢弃）
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert msgs[2]["content"] == "<|action_start|>B<|action_end|>"
    assert msgs[3]["content"][0]["text"] == "current game screen"


def test_encode_bgr_stdlib_png() -> None:
    # 强制 png：不依赖 PIL/numpy 的兜底路径可独立产出合法 PNG
    cfg = PolicyConfig(image_format="png")
    frame = bytes([0, 0, 255, 255, 0, 0, 0, 255, 0, 0, 0, 255])  # BGR 2×2
    url = encode_bgr(2, 2, frame, cfg)
    assert url.startswith("data:image/png;base64,")
    png = base64.b64decode(url.split(",", 1)[1])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (2, 2)  # IHDR 宽高


# ---------------------------------------------------------------- 代理配置与启动闸门

def test_policy_config_from_settings() -> None:
    cfg = policy_config_from_settings({})
    assert cfg.base_url == "http://127.0.0.1:8000/v1" and cfg.model == "gamescaling"
    assert cfg.reasoning is True and cfg.history_len == 5
    cfg = policy_config_from_settings({
        "gamescaling_url": "http://192.168.1.5:8000/v1/",
        "gamescaling_model": "my-model", "gamescaling_reasoning": False,
        "gamescaling_history_len": "3",
    })
    assert cfg.base_url == "http://192.168.1.5:8000/v1"  # 去尾斜杠
    assert cfg.model == "my-model" and cfg.reasoning is False
    assert cfg.history_len == 3
    bad = policy_config_from_settings({"gamescaling_history_len": "abc"})
    assert bad.history_len == 5  # 坏值回默认，不抛


def test_agent_start_refuses_bad_window_and_missing_prompt() -> None:
    async def _run() -> None:
        agent = GamescalingAgent()
        # 窗口未找到
        try:
            await agent.start(settings={}, window={"found": False, "hwnd": 0},
                              prompt_path=_DATA / "endfield_system_prompt.txt")
            raise AssertionError("应当拒绝")
        except AgentError as exc:
            assert "窗口" in str(exc)
        # 提示词文件缺失
        try:
            await agent.start(settings={"gamescaling_url": "http://127.0.0.1:9/v1"},
                              window={"found": True, "hwnd": 123, "title": "Endfield"},
                              prompt_path=_DATA / "nope.txt")
            raise AssertionError("应当拒绝")
        except AgentError as exc:
            assert "提示词" in str(exc)
        # 服务不可达（必拒端口，与 conftest 隔离策略一致；连接即时失败）
        try:
            await agent.start(settings={"gamescaling_url": "http://127.0.0.1:9/v1"},
                              window={"found": True, "hwnd": 123, "title": "Endfield"},
                              prompt_path=_DATA / "endfield_system_prompt.txt")
            raise AssertionError("应当拒绝")
        except AgentError as exc:
            assert "vLLM" in str(exc)
        assert not agent.running and agent.status()["running"] is False

    asyncio.run(_run())


# ---------------------------------------------------------------- executor probe（M6 行）

class _Suite:
    async def layer_enabled(self) -> bool:
        return False


def test_probe_modes_m6() -> None:
    async def _probe_ok() -> dict:
        return {"available": True, "detail": {"url": "http://x/v1"}}

    async def _boom() -> dict:
        raise RuntimeError("probe down")

    async def _run() -> None:
        base = dict(settings={"allow_run": True}, control_health=lambda: None,
                    install_probe=lambda: {"found": False}, suite=_Suite())
        # 不传 probe → 如实不可用
        out = await executor.probe_modes(**base)
        m6 = next(m for m in out["modes"] if m["mode"] == "m6")
        assert m6["available"] is False and "M6" in m6["reason"]
        # 传可用 probe → 透传
        out = await executor.probe_modes(**base, m6_probe=_probe_ok)
        m6 = next(m for m in out["modes"] if m["mode"] == "m6")
        assert m6["available"] is True and m6["detail"]["url"] == "http://x/v1"
        # probe 抛异常 → 降级不炸
        out = await executor.probe_modes(**base, m6_probe=_boom)
        m6 = next(m for m in out["modes"] if m["mode"] == "m6")
        assert m6["available"] is False and "探测失败" in m6["reason"]
        # M6 永不被 choose_mode 自动选中
        assert out["chosen"] == "m0"

    asyncio.run(_run())


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all gamescaling logic tests passed")
