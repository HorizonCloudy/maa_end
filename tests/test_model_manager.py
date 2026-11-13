"""maaend.model_manager 单测(纯标准库,不联网)。

覆盖:正常路径 / 边界 / 错误输入。下载用例用 file:// 避免真实网络。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maaend import model_manager as mm  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mm_test_"))

    # ---- 正常路径 ----
    f = tmp / "a.bin"
    f.write_bytes(b"hello maa_end")
    expect = hashlib.sha256(b"hello maa_end").hexdigest()
    check("sha256_file 正确", mm.sha256_file(f) == expect, mm.sha256_file(f))

    r = mm.verify_model(f, expect)
    check("verify 校验通过", r["ok"] is True, json.dumps(r, ensure_ascii=False))
    check("verify 返回 size", r["size"] == len(b"hello maa_end"))

    # ---- 边界：未登记期望值 → 通过但只给实际值 ----
    r2 = mm.verify_model(f, None)
    check("verify 无期望值时 ok=True 且 sha 有值",
          r2["ok"] is True and r2["sha256"] == expect, json.dumps(r2, ensure_ascii=False))
    check("verify 无期望值时 reason 提示未登记", "未登记" in (r2["reason"] or ""))

    # ---- 边界：空文件 ----
    empty = tmp / "empty.bin"
    empty.write_bytes(b"")
    r3 = mm.verify_model(empty, None)
    check("verify 空文件可读且 sha 为 e3b0c44…",
          r3["ok"] and r3["sha256"].startswith("e3b0c44298"), json.dumps(r3, ensure_ascii=False))

    # ---- 错误输入 1：文件不存在 ----
    r4 = mm.verify_model(tmp / "nope.pt", expect)
    check("verify 不存在 → ok=False", r4["ok"] is False and r4["exists"] is False,
          json.dumps(r4, ensure_ascii=False))
    check("verify 不存在 → reason 含'不存在'", "不存在" in (r4["reason"] or ""))

    # ---- 错误输入 2：SHA 不匹配 ----
    r5 = mm.verify_model(f, "0" * 64)
    check("verify SHA 不符 → ok=False", r5["ok"] is False, json.dumps(r5, ensure_ascii=False))
    check("verify SHA 不符 → reason 含'不匹配'", "不匹配" in (r5["reason"] or ""))

    # ---- 设备探测 ----
    dev = mm.detect_device()
    check("detect_device 返回合法值", dev in ("cuda", "cpu"), dev)

    # ---- ensure_model ----
    e = mm.ensure_model(str(f))
    check("ensure_model 绝对路径可用", e["ok"] is True, json.dumps(e, ensure_ascii=False))
    check("ensure_model 带 device/cache_dir",
          "device" in e and "cache_dir" in e and e["cache_dir"].endswith(mm.CACHE_DIRNAME))
    e2 = mm.ensure_model("definitely_missing_model.pt")
    check("ensure_model 缺失模型 → ok=False", e2["ok"] is False)

    # ---- 下载：SHA 不符 → 失败且不留残留 ----
    src = tmp / "src.bin"
    src.write_bytes(b"payload")
    dest = tmp / "downloaded.bin"
    res = mm.download_model(src.as_uri(), dest, "0" * 64)
    check("download SHA 不符 → ok=False", res["ok"] is False, json.dumps(res, ensure_ascii=False))
    check("download 失败不留 .part 残留",
          not dest.exists() and not dest.with_suffix(dest.suffix + ".part").exists())

    # ---- 下载：SHA 正确 → 成功落盘 ----
    good = hashlib.sha256(b"payload").hexdigest()
    dest2 = tmp / "ok.bin"
    res2 = mm.download_model(src.as_uri(), dest2, good)
    check("download SHA 正确 → ok=True", res2["ok"] is True, json.dumps(res2, ensure_ascii=False))
    check("download 成功后文件存在且内容一致",
          dest2.is_file() and dest2.read_bytes() == b"payload")

    # ---- 3.11 语法（宿主运行时是 3.11）----
    import ast

    src_txt = Path(mm.__file__).read_text(encoding="utf-8")
    try:
        ast.parse(src_txt, feature_version=(3, 11))
        check("源文件 3.11 语法兼容", True)
    except SyntaxError as exc:
        check("源文件 3.11 语法兼容", False, str(exc))

    print()
    if FAILS:
        print(f"结果：{len(FAILS)} 项失败 -> {FAILS}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
