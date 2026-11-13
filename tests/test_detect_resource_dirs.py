#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""detect._resource_dirs 自检：v2.35 平铺布局 + 旧版子目录布局。

跑法：python tests/test_detect_resource_dirs.py   （零依赖，不用 SDK）
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "maaend"))

from infra.probe import _resource_dirs  # noqa: E402


def test_resource_dirs_flat_and_nested():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "resource" / "pipeline").mkdir(parents=True)  # v2.35 平铺布局
        (root / "resource" / "base" / "pipeline").mkdir(parents=True)  # 旧版子目录布局
        (root / "resource" / "image").mkdir()
        assert _resource_dirs(root) == [str(root / "resource"), str(root / "resource" / "base")]


def test_resource_dirs_missing():
    with tempfile.TemporaryDirectory() as d:
        assert _resource_dirs(Path(d)) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
