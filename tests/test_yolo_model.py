#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""默认 YOLO 模型冒烟：类别数 / 类名 / 能前向，且与 meta.json 一致。

跑法：python tests/test_yolo_model.py  或 pytest（没装 ultralytics 时自动跳过）。

背景（2026-10-24 训练审计 #26）：插件只有 OCR 测试，默认 .pt 换版本 / 换模型后
没人校验类别表与 meta 是否一致，也没人发现「跨 ultralytics 版本加载后置信度崩塌」。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

YOLO_DIR = Path(__file__).resolve().parent.parent / "data" / "yolo"
PT = YOLO_DIR / "endfield_yolo_v2.pt"
META = YOLO_DIR / "endfield_yolo_v2.meta.json"
EXPECTED = ["enemy", "enemy_alert", "boss", "operator", "enemy_name",
            "operator_name", "item_name", "ui_button", "ui_icon"]

ultralytics = pytest.importorskip("ultralytics")
np = pytest.importorskip("numpy")


@pytest.fixture(scope="module")
def model():
    if not PT.is_file():
        pytest.skip(f"默认模型不存在: {PT}")
    from ultralytics import YOLO
    return YOLO(str(PT))


def test_class_names(model):
    assert [model.names[i] for i in range(len(model.names))] == EXPECTED


def test_meta_matches_model(model):
    if not META.is_file():
        pytest.skip("无 meta.json")
    text = META.read_text(encoding="utf-8")
    meta = json.loads(text)
    for name in EXPECTED:
        assert name in text, f"meta.json 缺类别 {name}"
    assert isinstance(meta, dict)


def test_predict_blank_frame(model):
    frame = np.zeros((640, 640, 3), dtype=np.uint8)
    res = model.predict(frame, conf=0.4, verbose=False, device="cpu")
    assert len(res) == 1  # 能前向即可；纯黑图不要求检出


def test_saved_version_matches_runtime(model):
    saved = (getattr(model, "ckpt", None) or {}).get("version")
    if not saved:
        pytest.skip("checkpoint 未记录版本")
    # 跨版本加载可能静默出垃圾置信度（审计 #3/#13）：版本不一致时显式失败提醒
    assert saved == ultralytics.__version__, (
        f"模型由 ultralytics {saved} 保存，当前 {ultralytics.__version__}")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
