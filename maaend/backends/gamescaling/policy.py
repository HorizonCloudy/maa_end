"""GameScaling 多轮策略客户端（vendor 自 THUSI-Lab/GameScaling，Apache-2.0，已适配）。

来源：gamescaling/policy.py @ 6764efabfbb4121d6e5d3cfcbc54dedf1469bf5a
许可：Apache-2.0，全文见 third_party/GameScaling-LICENSE.txt

对上游的适配（宿主是冻结 exe，插件全包零三方依赖，见 maaend/controlapi.py 同款约束）：

- ``requests.Session`` → 标准库 ``urllib``，并经 ``ProxyHandler({})`` 显式
  绕过系统代理（本机 vLLM 服务，同 p2p_http.py 的教训）；
- ``act(PIL.Image)`` → ``act_bgr(w, h, bgr_bytes)``：帧来自 yolo.capture_window
  的 24bpp BGR top-down DIB。编码链：PIL 可导入则 JPEG（上游默认，体积小），
  否则标准库 zlib+struct 的 PNG 兜底；BGR→RGB 优先 numpy（宿主随 onnxruntime
  自带），否则纯 Python。
- 请求体、消息结构、预填与 finalize 语义逐字保留——训练格式敏感。

一次 :meth:`GameScalingPolicy.act_bgr` 调用是一个 200ms 决策：

    system            该游戏的系统提示词（见 ``gamescaling.prompt``）
    user, assistant   每个历史决策一对："current game screen" + 帧，
    ...               然后是模型执行的动作（reasoning 默认丢弃，
                      除非 ``history_think`` 要求保留）
    user              "current game screen" + 当前帧
    assistant         预填 "<think>"（reasoning=True）或
                      "<think>\\n\\n</think>"（reasoning=False）；模型续写

checkpoint 自带的 chat template 把无推理的历史 assistant 轮渲染为
``<think>\\n\\n</think>\\n\\n<|action_start|>...``，与训练逐 token 一致，
所以客户端只发送动作字符串。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import struct
import time
import urllib.error as _ue
import urllib.request as _rq
import zlib
from collections import deque
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from .action import (
    ACTION_END,
    ACTION_START,
    THINK_OPEN,
    Action,
    parse_action,
    split_reasoning,
)
from .prompt import USER_TURN_TEXT

logger = logging.getLogger(__name__)

# 显式绕过 HTTP(S)_PROXY / 系统代理：vLLM 服务按本机/局域网部署（p2p_http 同款）。
_OPENER = _rq.build_opener(_rq.ProxyHandler({}))

# Assistant 预填。两种模式用同一份系统提示词；空 think 块正是训练里
# 无推理轮的渲染方式。
THINK_PREFILL = THINK_OPEN
NO_THINK_PREFILL = "<think>\n\n</think>"


@dataclass
class PolicyConfig:
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = "gamescaling"
    api_key: str = "EMPTY"
    # 上下文保留的历史决策数（每条 = 1 帧 + 1 动作）。
    history_len: int = 5
    # 最近多少个历史轮保留 <think> 文本；-1 = 全部。
    history_think: int = 0
    # 发送给模型的帧尺寸。None = 原样发送（本插件默认，截多大发多大）。
    # 缩放需要 PIL；无 PIL 的宿主上设置非 None 会在启动自检时报错。
    image_size: tuple[int, int] | None = None
    image_format: str = "auto"          # auto | jpeg | png
    jpeg_quality: int = 90
    # True：预填 "<think>"，模型自己决定是否思考、思考多久。
    # False：预填空 think 块，模型直出动作。
    reasoning: bool = True
    temperature: float = 1.0
    max_tokens: int = 2048
    stop: list[str] = field(default_factory=lambda: [ACTION_END])
    # 额外恢复畸形回复（未闭合 <think> 里的动作、只有键的骨架）。默认关。
    lenient_parse: bool = False
    timeout: float = 120.0

    @property
    def prefill(self) -> str:
        return THINK_PREFILL if self.reasoning else NO_THINK_PREFILL


class Turn(NamedTuple):
    image_url: str
    reasoning: str
    action_text: str


@dataclass
class Decision:
    """一次 :meth:`GameScalingPolicy.act_bgr` 调用的结果。"""

    action: Action
    text: str
    reasoning: str
    latency_ms: float
    finish_reason: str | None = None
    completion_tokens: int | None = None
    error: str | None = None


# ---------------------------------------------------------------- 帧编码

def _bgr_to_rgb(bgr: bytes, width: int, height: int) -> bytes:
    """24bpp BGR → RGB。numpy（宿主随 onnxruntime 自带）优先，纯 Python 兜底。"""
    try:
        import numpy as np

        arr = np.frombuffer(bgr, dtype=np.uint8).reshape(height, width, 3)
        return arr[:, :, ::-1].tobytes()
    except ImportError:
        rgb = bytearray(len(bgr))
        for i in range(0, len(bgr), 3):
            rgb[i], rgb[i + 1], rgb[i + 2] = bgr[i + 2], bgr[i + 1], bgr[i]
        return bytes(rgb)


def _png_bytes(rgb: bytes, width: int, height: int) -> bytes:
    """标准库 PNG 编码（zlib+struct，8bit 真彩）：无 PIL 宿主的兜底路径。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit, color type 2
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def probe_encoder(cfg: PolicyConfig) -> str:
    """启动自检：2×2 帧走一遍编码链，返回编码器描述（"jpeg(PIL)" / "png(stdlib)"）。"""
    frame = bytes([0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255])  # BGR 2×2
    url = encode_bgr(2, 2, frame, cfg)
    return "jpeg(PIL)" if url.startswith("data:image/jpeg") else "png(stdlib)"


def encode_bgr(width: int, height: int, bgr: bytes, cfg: PolicyConfig) -> str:
    """截图 BGR 帧重采样并编码为请求携带的 data URL。"""
    if cfg.image_size is not None and (width, height) != tuple(cfg.image_size):
        try:
            from PIL import Image
        except ImportError as exc:
            raise ValueError(
                "image_size 需要 PIL 缩放，本机宿主没有 PIL；"
                "保持 gamescaling_image_size 未设（发原生分辨率）即可。"
            ) from exc
        rgb = _bgr_to_rgb(bgr, width, height)
        img = Image.frombytes("RGB", (width, height), rgb).resize(
            tuple(cfg.image_size), Image.BILINEAR)
        buf_rgb, w, h = img.tobytes(), img.size[0], img.size[1]
    else:
        buf_rgb, w, h = _bgr_to_rgb(bgr, width, height), width, height

    want = cfg.image_format
    if want == "auto" or want == "jpeg":
        try:
            import io

            from PIL import Image

            out = io.BytesIO()
            Image.frombytes("RGB", (w, h), buf_rgb).save(out, format="JPEG",
                                                         quality=cfg.jpeg_quality)
            return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
        except ImportError:
            if want == "jpeg":
                raise ValueError("image_format=jpeg 需要 PIL，本机宿主没有；用 auto/png。")
    png = _png_bytes(buf_rgb, w, h)
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


# ---------------------------------------------------------------- 消息与请求

def user_turn(image_url: str) -> dict:
    return {"role": "user", "content": [
        {"type": "text", "text": USER_TURN_TEXT},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]}


def assistant_turn(action_text: str, reasoning: str = "") -> dict:
    if reasoning:
        action_text = f"{THINK_OPEN}\n{reasoning.strip()}\n</think>\n\n{action_text}"
    return {"role": "assistant", "content": action_text}


def build_request(messages: list[dict], cfg: PolicyConfig) -> dict:
    """OpenAI 兼容 /chat/completions 请求体，含我们依赖的 vLLM 扩展。"""
    body: dict[str, Any] = {
        "model": cfg.model,
        "messages": list(messages),
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        # 动作 tag 是特殊 token；解码文本里要保留它们。
        "skip_special_tokens": False,
    }
    if cfg.stop:
        body["stop"] = list(cfg.stop)
    body["messages"].append({"role": "assistant", "content": cfg.prefill})
    body["continue_final_message"] = True
    body["add_generation_prompt"] = False
    return body


def finalize(text: str, finish_reason: str | None, cfg: PolicyConfig) -> str:
    """撤销请求本身对回复做的事。

    返回文本不含预填，stop 串也被从中剥掉。两者都补回来，
    让回复的解析方式与训练数据一致。
    """
    text = text or ""
    if not text.lstrip().startswith(THINK_OPEN):
        text = cfg.prefill + text
    _, tail, closed = split_reasoning(text)
    if closed and ACTION_START not in tail:
        bare = tail.strip()
        if re.match(r"^-?\d", bare) and ";" in bare:
            text = text[: len(text) - len(tail)] + ACTION_START + bare
    if (finish_reason == "stop" and ACTION_START in text
            and ACTION_END not in text[text.rfind(ACTION_START):]):
        text += ACTION_END
    return text


class GameScalingPolicy:
    """有状态客户端：回合开始调 :meth:`reset`，每 200ms 调一次 :meth:`act_bgr`。"""

    def __init__(self, system_prompt: str, config: PolicyConfig | None = None):
        self.system_prompt = system_prompt
        self.cfg = config or PolicyConfig()
        self._history: deque[Turn] = deque()

    @property
    def base_url(self) -> str:
        return self.cfg.base_url.rstrip("/")

    def reset(self) -> None:
        self._history.clear()

    # ------------------------------------------------------------ HTTP

    def _post(self, path: str, body: dict, timeout: float) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = _rq.Request(
            self.base_url + path, data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.cfg.api_key}"},
            method="POST",
        )
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                raw = resp.read(64 << 20)
        except _ue.HTTPError as exc:
            detail = exc.read(1 << 20).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail[:500]}") from exc
        except Exception as exc:  # noqa: BLE001 - 连接失败等统一成 RuntimeError
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise RuntimeError(f"响应不是 JSON: {raw[:120]!r}") from exc

    def health_check(self, timeout: float = 3.0) -> bool:
        req = _rq.Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.cfg.api_key}"})
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return resp.status < 400
        except Exception:  # noqa: BLE001 - 不可达即 False，不抛
            return False

    # ------------------------------------------------------------ 决策

    def messages(self, image_url: str) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": self.system_prompt}] if self.system_prompt else []
        n = len(self._history)
        for i, turn in enumerate(self._history):
            out.append(user_turn(turn.image_url))
            age = n - i
            keep = self.cfg.history_think < 0 or age <= self.cfg.history_think
            out.append(assistant_turn(turn.action_text, turn.reasoning if keep else ""))
        out.append(user_turn(image_url))
        return out

    def act_bgr(self, width: int, height: int, bgr: bytes) -> Decision:
        """截图帧（capture_window 的 BGR 输出）→ 一次 200ms 决策。永不抛异常。"""
        try:
            url = encode_bgr(width, height, bgr, self.cfg)
        except ValueError as exc:
            logger.error("frame encode failed: %s", exc)
            return Decision(action=Action(), text="", reasoning="", latency_ms=0.0, error=str(exc))
        body = build_request(self.messages(url), self.cfg)
        t0 = time.perf_counter()
        text, finish, tokens, error = "", None, None, None
        try:
            payload = self._post("/chat/completions", body, self.cfg.timeout)
            choice = (payload.get("choices") or [{}])[0]
            text = (choice.get("message") or {}).get("content") or ""
            finish = choice.get("finish_reason")
            tokens = (payload.get("usage") or {}).get("completion_tokens")
        except Exception as exc:  # noqa: BLE001 - 请求失败如实进 Decision.error
            error = str(exc)
            logger.error("policy request failed: %s", exc)
        latency_ms = (time.perf_counter() - t0) * 1000
        text = finalize(text, finish, self.cfg) if not error else ""
        action = parse_action(text, lenient=self.cfg.lenient_parse)
        reasoning, _, closed = split_reasoning(text)
        self._history.append(Turn(url, reasoning if closed else "", action.history_text))
        while len(self._history) > self.cfg.history_len:
            self._history.popleft()
        return Decision(action=action, text=text, reasoning=reasoning, latency_ms=latency_ms,
                        finish_reason=finish, completion_tokens=tokens, error=error)
