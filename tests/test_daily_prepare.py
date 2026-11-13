#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daily_orchestrator.all_tasks 自检：读任务表前必须先刷新 pi-runner（首次运行 / 换版本）。

跑法：python tests/test_daily_prepare.py   （零依赖，不用 SDK；prepare_runner 打桩，不建 junction）
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "data" / "yolo"))

import daily_orchestrator  # noqa: E402
import maa_scenes  # noqa: E402


def test_all_tasks_prepares_first():
    with tempfile.TemporaryDirectory() as d:
        iface = Path(d) / "interface.json"
        daily_orchestrator.IFACE = iface

        def fake_prepare():  # 模拟 prepare() 生成合并版 interface.json
            iface.write_text(json.dumps({"task": [{"name": "AutoCollect"}]}), encoding="utf-8")
            return object()

        maa_scenes.prepare_runner = fake_prepare
        assert daily_orchestrator.all_tasks() == ["AutoCollect"]


def test_all_tasks_empty_when_prepare_fails():
    with tempfile.TemporaryDirectory() as d:
        iface = Path(d) / "interface.json"
        iface.write_text(json.dumps({"task": [{"name": "Stale"}]}), encoding="utf-8")
        daily_orchestrator.IFACE = iface
        maa_scenes.prepare_runner = lambda: None  # 安装缺失：不能拿旧任务表冒充
        assert daily_orchestrator.all_tasks() == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
