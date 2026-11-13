"""AssistConfig 双配置（A/B 档）单测。

覆盖:正常路径 / 边界(老配置兼容、非法档回落) / 错误输入(非法值吸附)。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maaend.assist import (  # noqa: E402
    CONFIDENCES,
    CONSECUTIVES,
    MODES,
    PROFILES,
    AssistConfig,
    _norm_conf,
    _norm_consec,
    _norm_mode,
    profile_key,
)

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def main() -> int:
    # ---- 正常路径：双配置 ----
    s = {
        "assist_profile": "B",
        "assist_profile_a_mode": "宏观建议",
        "assist_profile_a_confidence": 0.85,
        "assist_profile_a_consecutive": 3,
        "assist_profile_b_mode": "精细建议",
        "assist_profile_b_confidence": 0.70,
        "assist_profile_b_consecutive": 5,
    }
    c = AssistConfig.from_settings(s)
    check("生效档 = B", c.profile == "B", c.profile)
    check("生效值取 B 的", (c.assist_mode, c.confidence_threshold, c.consecutive_fail_threshold)
          == ("精细建议", 0.70, 5), f"{c.assist_mode}/{c.confidence_threshold}/{c.consecutive_fail_threshold}")
    check("两档都解析出来供面板显示", set(c.profiles.keys()) == {"A", "B"}, str(list(c.profiles)))
    check("A 档值保留", c.profiles["A"]["assist_mode"] == "宏观建议")

    # ---- 边界 1：老配置（无 profile 键）→ 兼容 ----
    old = {"assist_mode": "精细建议", "confidence_threshold": 0.90, "consecutive_fail_threshold": 2}
    c2 = AssistConfig.from_settings(old)
    check("老配置默认档 A", c2.profile == "A", c2.profile)
    check("老配置值生效", (c2.assist_mode, c2.confidence_threshold, c2.consecutive_fail_threshold)
          == ("精细建议", 0.90, 2))
    check("老配置下两档都回落为同一套值",
          c2.profiles["A"]["assist_mode"] == "精细建议" and c2.profiles["B"]["assist_mode"] == "精细建议")

    # ---- 边界 2：只配了 A，没配 B → B 用老扁平值兜底 ----
    s3 = {"assist_profile": "B", "assist_profile_a_mode": "精细建议", "assist_mode": "宏观建议"}
    c3 = AssistConfig.from_settings(s3)
    check("B 未配时用旧扁平键兜底", c3.assist_mode == "宏观建议", c3.assist_mode)
    check("A 保留自己的值", c3.profiles["A"]["assist_mode"] == "精细建议")

    # ---- 错误输入 1：非法档位 → 回落 A ----
    c4 = AssistConfig.from_settings({"assist_profile": "Z"})
    check("非法档回落 A", c4.profile == "A", c4.profile)
    c5 = AssistConfig.from_settings({"assist_profile": ""})
    check("空档回落 A", c5.profile == "A", c5.profile)

    # ---- 错误输入 2：非法值吸附到最近合法档 ----
    check("_norm_mode 非法 → 回落", _norm_mode("乱写") == "宏观建议")
    check("_norm_conf 0.83 → 0.85", _norm_conf(0.83) == 0.85, str(_norm_conf(0.83)))
    check("_norm_conf 非数字 → 0.85", _norm_conf("abc") == 0.85)
    check("_norm_conf NaN → 0.85", _norm_conf(float("nan")) == 0.85)
    check("_norm_consec 4 → 3 或 5(最近)", _norm_consec(4) in CONSECUTIVES, str(_norm_consec(4)))
    check("_norm_consec 非数字 → 3", _norm_consec(None) == 3)

    # ---- profile_key ----
    check("profile_key A/mode", profile_key("A", "assist_mode") == "assist_profile_a_mode")
    check("profile_key B/confidence",
          profile_key("B", "confidence_threshold") == "assist_profile_b_confidence")
    check("profile_key 大小写不敏感", profile_key("b", "assist_mode") == "assist_profile_b_mode")
    try:
        profile_key("A", "nope")
        check("profile_key 未知参数抛 KeyError", False)
    except KeyError:
        check("profile_key 未知参数抛 KeyError", True)

    # ---- as_settings_patch 回写 ----
    patch = c.as_settings_patch()
    check("patch 带 assist_profile=B", patch.get("assist_profile") == "B")
    check("patch 含 B 的三个键",
          all(profile_key("B", p) in patch for p in
              ("assist_mode", "confidence_threshold", "consecutive_fail_threshold")))
    # 回写后再解析应保持一致
    merged = dict(s)
    merged.update(patch)
    c6 = AssistConfig.from_settings(merged)
    check("patch 回写后往返一致",
          (c6.profile, c6.assist_mode, c6.confidence_threshold, c6.consecutive_fail_threshold)
          == (c.profile, c.assist_mode, c.confidence_threshold, c.consecutive_fail_threshold))

    # ---- 常量完整性 ----
    check("MODES/CONFIDENCES/CONSECUTIVES 非空",
          bool(MODES) and bool(CONFIDENCES) and bool(CONSECUTIVES))
    check("PROFILES = (A,B)", PROFILES == ("A", "B"), str(PROFILES))

    # ---- 3.11 语法 ----
    try:
        ast.parse(Path(sys.modules["maaend.assist"].__file__).read_text(encoding="utf-8"),
                  feature_version=(3, 11))
        check("assist.py 3.11 语法兼容", True)
    except SyntaxError as exc:
        check("assist.py 3.11 语法兼容", False, str(exc))

    print()
    if FAILS:
        print(f"结果：{len(FAILS)} 项失败 -> {FAILS}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
