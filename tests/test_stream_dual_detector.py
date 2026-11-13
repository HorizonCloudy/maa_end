"""StreamService 双 detector 合并逻辑。

覆盖 `_detect_both` 的四条路径（2026-11-05 接入干员专精模型时新增）：
1. 未配置专精模型 → 只有主路结果，标记 source=main
2. 配置了专精模型 → 专精结果**排在前面**，各自带 source
3. 专精模型报错 → 主路结果完整保留，额外附一条 error 标记（不吞、不崩）
4. 主路失败 → 原样返回失败，不尝试专精（避免把错误放大成两条）

用 `__new__` 绕过 `__init__`：这里只测纯逻辑，不需要日志线程与真实 sidecar。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 与其它纯逻辑测试一致：把插件根挂进 sys.path，按 `maaend.*` 导入，
# 不依赖宿主目录名（宿主目录是 maa_end，包名是 maaend）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from maaend.stream import StreamService  # noqa: E402


class _FakeDetector:
    def __init__(self, resp: dict):
        self.resp = resp
        self.calls = 0

    def detect_hwnd(self, hwnd: int) -> dict:
        self.calls += 1
        if isinstance(self.resp, Exception):
            raise self.resp
        return dict(self.resp)


def _svc(main: dict, operator: dict | Exception | None = None) -> StreamService:
    svc = StreamService.__new__(StreamService)
    svc._detector = _FakeDetector(main)
    svc._detector_operator = _FakeDetector(operator) if operator is not None else None
    return svc


def test_without_operator_model_only_main() -> None:
    svc = _svc({"ok": True, "objects": [{"label": "enemy"}, {"label": "operator"}], "size": [1, 1]})
    out = svc._detect_both(1)
    assert out["ok"] is True
    assert out["operator_enabled"] is False
    assert out["count"] == 2
    assert all(o["source"] == "main" for o in out["objects"])


def test_operator_results_come_first() -> None:
    svc = _svc(
        {"ok": True, "objects": [{"label": "operator", "confidence": 0.6}], "size": [1, 1]},
        {"ok": True, "objects": [{"label": "operator", "confidence": 0.9}]},
    )
    out = svc._detect_both(1)
    assert out["operator_enabled"] is True
    assert out["count"] == 2
    # 专精模型精度更高，排前面 → 下游取第一个时拿到更准的
    assert out["objects"][0]["source"] == "operator"
    assert out["objects"][0]["confidence"] == 0.9
    assert out["objects"][1]["source"] == "main"


def test_operator_error_does_not_break_main() -> None:
    svc = _svc({"ok": True, "objects": [{"label": "enemy"}], "size": [1, 1]},
               {"ok": False, "error": "sidecar 通信失败"})
    out = svc._detect_both(1)
    assert out["ok"] is True
    labels = [o.get("label") for o in out["objects"]]
    assert "enemy" in labels                      # 主路结果没丢
    assert "operator_model_error" in labels       # 专精失败被如实记录
    err = next(o for o in out["objects"] if o["label"] == "operator_model_error")
    assert err["source"] == "operator" and "sidecar" in err["error"]


def test_operator_exception_is_contained() -> None:
    svc = _svc({"ok": True, "objects": [], "size": [1, 1]}, RuntimeError("boom"))
    out = svc._detect_both(1)
    assert out["ok"] is True
    assert any(o.get("label") == "operator_model_error" for o in out["objects"])


def test_main_failure_returns_early() -> None:
    svc = _svc({"ok": False, "error": "窗口不在线"}, {"ok": True, "objects": [{"label": "operator"}]})
    out = svc._detect_both(1)
    assert out == {"ok": False, "error": "窗口不在线"}
    assert svc._detector_operator.calls == 0      # 主路失败就不该再问专精
