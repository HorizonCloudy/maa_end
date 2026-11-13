"""帧格式转换 —— BGR 原始字节与 numpy 数组之间的唯一入口。

背景：`router._assist_scene_probe_sync` 与 `router._scene_now` 各自内联了
`np.frombuffer → reshape → [:, :, ::-1] → ascontiguousarray` 这四步，
入口层因此拖着 numpy 依赖（docs/REFACTOR-PLAN.md §2 #4）。

`gamescaling/policy.py` 里还有一份 `_bgr_to_rgb` —— 那是 vendor 代码，
为避免与上游 diff 变大，**不并进这里**。
"""

from __future__ import annotations

from typing import Any


def to_ndarray(width: int, height: int, bgr: bytes) -> Any:
    """BGR 字节 → (h, w, 3) uint8 数组（BGR 通道顺序）。"""
    import numpy as np

    return np.frombuffer(bgr, dtype=np.uint8).reshape(height, width, 3)


def to_rgb(width: int, height: int, bgr: bytes) -> Any:
    """BGR 字节 → (h, w, 3) uint8 数组（RGB 通道顺序，C 连续）。

    `.copy()` 是必需的：`[:, :, ::-1]` 是负步长视图，PIL/onnxruntime 拿到
    非连续数组会各自踩坑（PIL 会慢一个量级，ort 部分版本直接报错）。
    """
    frame = to_ndarray(width, height, bgr)
    return frame[:, :, ::-1].copy()
