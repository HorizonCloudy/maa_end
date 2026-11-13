#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OCR 层纯逻辑自检：本体匹配规则 / sidecar 协议 / 中文路径读图 / 客户端错误包。

跑法：python tests/test_ocr_logic.py
（不依赖 SDK；协议测试拉真 sidecar 子进程但只到 banner/ping，不加载 OCR 引擎，
秒级完成。引擎级识别冒烟另见 data/yolo/ 下的实测流程。）
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SIDECAR = ROOT / "maaend" / "ocr_sidecar.py"
_CLIENT = ROOT / "maaend" / "ocr.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _write_ontology(path: Path) -> None:
    path.write_text(json.dumps({
        "name_index": {
            "白垩界卫": {"kind": "enemy"},
            "白垩界卫（沉眠）": {"kind": "enemy"},
            "50式应龙短刃": {"kind": "weapon"},
            "潜地虬兽": {"kind": "enemy"},
        }
    }, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 匹配规则

def test_match() -> None:
    m = _load_module(_SIDECAR, "ocr_sidecar_test")
    with tempfile.TemporaryDirectory() as td:
        onto = Path(td) / "onto.json"
        _write_ontology(onto)
        assert m.load_ontology(str(onto)) == 4
    assert m._match("白垩界卫") == ("白垩界卫", "enemy_name")            # 精确
    assert m._match("白垩界卫（沉眠）") == ("白垩界卫（沉眠）", "enemy_name")  # 状态变体
    assert m._match("潜地虬兽×3") == ("潜地虬兽", "enemy_name")          # 子串(计数后缀)
    assert m._match("白垩界卫沉") == ("白垩界卫", "enemy_name")          # 尾部多 1 字噪声
    assert m._match("50式应龙短忍") == ("50式应龙短刃", "item_name")      # 1 字错噪声
    assert m._match("垩界卫") is None                                   # 首字不同:按设计拒绝
    assert m._match("完全无关词") is None
    assert m.load_ontology(str(Path(td := tempfile.mkdtemp()) / "无")) == 0  # 缺文件静默
    print("  匹配规则 OK")


# ---------------------------------------------------------------- handle 错误路径

def test_handle_error_paths() -> None:
    m = _load_module(_SIDECAR, "ocr_sidecar_test2")
    r = m.handle({"cmd": "nope"})
    assert not r["ok"] and "未知 cmd" in r["error"]
    r = m.handle({"cmd": "ocr", "hwnd": 0})
    assert not r["ok"] and "hwnd" in r["error"]
    r = m.handle({"cmd": "find", "hwnd": 1, "text": "  "})
    assert not r["ok"] and "text" in r["error"]
    r = m.handle({"cmd": "ocr_image", "path": ""})
    assert not r["ok"] and "path" in r["error"]
    print("  handle 错误路径 OK")


# ---------------------------------------------------------------- find 归一化

def test_find_normalized() -> None:
    """带空格的 needle 也要按"完全相等"优先级命中(find 归一化比较)。"""
    m = _load_module(_SIDECAR, "ocr_sidecar_test3")
    lines = [
        {"text": "开始行动X", "conf": 0.8, "box": [0, 0, 5, 4], "center": [2, 2],
         "tag": "", "cls": ""},
        {"text": "开始行动", "conf": 0.9, "box": [0, 0, 4, 4], "center": [2, 2],
         "tag": "", "cls": ""},
    ]
    m._from_hwnd = lambda hwnd, *_a, **_k: (lines, [100, 100], [0, 0])  # 屏蔽截图+引擎
    r = m.handle({"cmd": "find", "hwnd": 1, "text": "开始 行动"})
    assert r["found"] and r["match"]["text"] == "开始行动", r
    # 本体命中(tag)也参与归一化优选
    lines2 = [{"text": "开启行动", "conf": 0.9, "box": [0, 0, 4, 4], "center": [2, 2],
               "tag": "开始行动", "cls": "enemy_name"}]
    m._from_hwnd = lambda hwnd, *_a, **_k: (lines2, [100, 100], [0, 0])
    r = m.handle({"cmd": "find", "hwnd": 1, "text": "开始行动"})
    assert r["match"]["tag"] == "开始行动", r
    print("  find 归一化 OK")


# ---------------------------------------------------------------- 中文路径读图

def test_chinese_path() -> None:
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("  [skip] 无 cv2,跳过中文路径读图")
        return
    m = _load_module(_SIDECAR, "ocr_sidecar_test4")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "测试截图.png"
        ok, buf = cv2.imencode(".png", np.zeros((8, 8, 3), dtype=np.uint8))
        assert ok
        buf.tofile(str(p))
        img = m._load_image(str(p))  # 旧实现 cv2.imread 在这里返回 None
        assert img is not None and img.shape == (8, 8, 3), "中文路径读图失败"
    print("  中文路径读图 OK")


# ---------------------------------------------------------------- sidecar 协议(真子进程)

def _ask(proc, req: dict) -> dict:
    proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def test_sidecar_protocol() -> None:
    with tempfile.TemporaryDirectory() as td:
        onto = Path(td) / "onto.json"
        _write_ontology(onto)
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(_SIDECAR), "0.55", str(onto)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            banner = json.loads(proc.stdout.readline())
            assert banner["ok"] and banner["index"] == 4, banner
            assert _ask(proc, {"cmd": "ping"})["pong"] is True
            r = _ask(proc, {"cmd": "nope"})
            assert not r["ok"] and "未知 cmd" in r["error"]
            r = _ask(proc, {"cmd": "ocr", "hwnd": 0})
            assert not r["ok"] and "hwnd" in r["error"]  # 不触引擎,秒回
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)
    print("  sidecar 协议 OK(banner/ping/错误路径)")


# ---------------------------------------------------------------- 客户端

def test_client() -> None:
    m_cli = _load_module(_CLIENT, "ocr_client_test")
    _load_module(_SIDECAR, "ocr_sidecar_test5")  # 只为验证 sidecar 模块可加载
    with tempfile.TemporaryDirectory() as td:
        onto = Path(td) / "onto.json"
        _write_ontology(onto)
        # 正常路径:banner + ping
        c = m_cli.SidecarOcr(ontology=str(onto), python_exe=sys.executable)
        try:
            assert c.ping() is True
        finally:
            c.stop()
        # 启动失败:python_exe 指向非可执行文件 → 错误包而非抛异常
        bad = m_cli.SidecarOcr(python_exe=str(onto))  # .json 当解释器,WinError 193
        try:
            assert bad.ping() is False  # ping 契约:bool
            r = bad.read_image(str(onto))  # 文件存在才会走到 _ask
            assert r["ok"] is False and "启动失败" in r["error"], r
        finally:
            bad.stop()
    print("  客户端 OK(ping / 启动失败错误包)")


if __name__ == "__main__":
    test_match()
    test_handle_error_paths()
    test_find_normalized()
    test_chinese_path()
    test_sidecar_protocol()
    test_client()
    print("test_ocr_logic 全部通过")
