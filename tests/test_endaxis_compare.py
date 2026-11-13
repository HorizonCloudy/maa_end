"""排轴对照（endaxis.compare）的行为。

覆盖 4 条路径（2026-11-06 新增 compare action）：
1. 自往返：同一份轴对比自己，diff 全 0（证明编码/解码/统计链路闭环）
2. 跨预设：BOSS 码 vs 开荒预设，diff 非 0（证明对比真的在比，不是恒等返回）
3. 非法码：抛 ValueError 且信息可读（不能抛 BadGzipFile 这种底层异常）
4. 空码：抛 ValueError（而不是静默返回空结果）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maaend import endaxis as ea  # noqa: E402


def test_self_roundtrip_diff_is_zero() -> None:
    code = ea.encode(ea.build_project("BOSS", 4))
    out = ea.compare(code, "BOSS", 4)
    assert out["diff"] == {"actions": 0, "scenarios": 0, "ultimate": 0}
    assert out["community"]["actions"] == out["preset"]["actions"] > 0


def test_cross_preset_diff_nonzero() -> None:
    code = ea.encode(ea.build_project("BOSS", 4))
    out = ea.compare(code, "开荒", 4)
    # BOSS 场景更多、动作更多、终结技更多
    assert out["diff"]["actions"] > 0
    assert out["diff"]["scenarios"] > 0
    assert out["diff"]["ultimate"] > 0
    assert out["community"]["by_type"]["ultimate"] == 5


def test_invalid_code_raises_readable_error() -> None:
    with pytest.raises(ValueError) as ei:
        ea.compare("not-a-real-code", "开荒")
    msg = str(ei.value)
    assert "解析失败" in msg
    assert "base64url" in msg  # 提示期望格式，而不是只抛底层异常


def test_empty_code_raises() -> None:
    with pytest.raises(ValueError):
        ea.compare("", "开荒")


def test_members_truncates_slots() -> None:
    """members=2 时，预设只保留 0/1 号位动作。"""
    out = ea.compare(ea.encode(ea.build_project("开荒", 2)), "开荒", 2)
    assert set(out["preset"]["by_slot"]) <= {"0", "1"}
