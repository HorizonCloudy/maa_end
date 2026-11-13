"""低延迟链路单测：LatestWinsDetector 的 latest-wins 语义 + 引擎缓存键。

覆盖:正常路径 / 边界(单请求) / 错误输入(runner 抛异常)。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maaend import model_manager as mm  # noqa: E402
from maaend.yolo import LatestWinsDetector  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def main() -> int:
    # ---------- 正常路径：单个请求能拿到结果 ----------
    lw = LatestWinsDetector(lambda req: {"ok": True, "echo": req})
    r = lw.submit({"cmd": "detect", "hwnd": 1})
    check("单请求返回 runner 结果", r.get("ok") is True and r.get("echo", {}).get("hwnd") == 1, str(r))

    # ---------- 正常路径：串行多请求各自拿到自己的结果 ----------
    outs = [lw.submit({"cmd": "detect", "hwnd": i}) for i in range(5)]
    check("串行 5 个请求全部返回",
          all(o.get("ok") for o in outs), str([o.get("ok") for o in outs]))

    # ---------- 核心：新请求覆盖旧请求 → 旧请求 superseded ----------
    gate = threading.Event()
    started = threading.Event()
    processed: list[int] = []

    def slow(req):
        started.set()
        gate.wait(5)                      # 卡住,制造"处理慢于到达"的场景
        processed.append(req["hwnd"])
        return {"ok": True, "hwnd": req["hwnd"]}

    lw2 = LatestWinsDetector(slow)
    res_holder: dict[int, dict] = {}

    def caller(seq):
        res_holder[seq] = lw2.submit({"cmd": "detect", "hwnd": seq})

    t1 = threading.Thread(target=caller, args=(100,))
    t1.start()
    started.wait(5)                       # 等 worker 开始处理 100

    # 100 还在处理中,此刻连续提交 101/102/103 → 信箱只留最新(103)
    t2 = threading.Thread(target=caller, args=(101,))
    t3 = threading.Thread(target=caller, args=(102,))
    t4 = threading.Thread(target=caller, args=(103,))
    for t in (t2, t3, t4):
        t.start()
    time.sleep(0.3)                       # 让 101/102 被 103 覆盖
    gate.set()                            # 放行

    for t in (t1, t2, t3, t4):
        t.join(10)

    # 语义：一旦有更新请求提交,在途的 100 也判 superseded(其结果出来时
    # 已陈旧,让调用方立刻放行,而不是干等一个过期结果)。最新 103 必得结果。
    superseded = [k for k, v in res_holder.items() if v.get("superseded")]
    ok_ones = [k for k, v in res_holder.items() if v.get("ok")]
    check("在途的 100 与过期的 101/102 都判 superseded",
          set(superseded) == {100, 101, 102}, f"superseded={superseded} ok={ok_ones}")
    check("最新请求 103 必拿到真实结果(无活锁)",
          set(ok_ones) == {103}, f"ok={ok_ones} processed={processed}")
    check("101/102 未被真正计算(runner 只跑了 100 和 103)",
          processed == [100, 103], str(processed))

    # ---------- 错误输入：runner 抛异常 → 返回 ok=False,线程不死 ----------
    lw3 = LatestWinsDetector(lambda req: (_ for _ in ()).throw(RuntimeError("boom")))
    r3 = lw3.submit({"cmd": "detect"})
    check("runner 异常 → 返回 ok=False", r3.get("ok") is False, str(r3))
    check("runner 异常 → error 带上原因", "boom" in str(r3.get("error")))
    r3b = lw3.submit({"cmd": "detect"})
    check("异常后线程仍存活(第二次仍返回)", r3b.get("ok") is False, str(r3b))

    # ---------- 边界：stop() 后 pending 语义正常 ----------
    lw4 = LatestWinsDetector(lambda req: {"ok": True})
    check("初始 pending=False", lw4.pending is False)
    lw4.stop()
    check("stop() 不抛异常", True)

    # ---------- 引擎缓存键 ----------
    k1 = mm.engine_cache_key("abc123", 640, "cuda", True)
    k2 = mm.engine_cache_key("abc123", 640, "cuda", True)
    k3 = mm.engine_cache_key("abc123", 960, "cuda", True)
    k4 = mm.engine_cache_key("abc123", 640, "cpu", True)
    k5 = mm.engine_cache_key("abc123", 640, "cuda", False)
    check("同参数键稳定", k1 == k2)
    check("imgsz 变 → 键变", k1 != k3)
    check("device 变 → 键变", k1 != k4)
    check("fp16/fp32 变 → 键变", k1 != k5)
    check("键长度 16(短十六进制)", len(k1) == 16 and all(c in "0123456789abcdef" for c in k1), k1)
    p = mm.engine_path("abc123", 640, "cuda", True)
    check("engine_path 落在 .cache 下且以 .engine 结尾",
          p.name.endswith(".engine") and p.parent.name == mm.CACHE_DIRNAME, str(p))

    # ---------- 3.11 语法 ----------
    import ast

    for mod in (mm, sys.modules["maaend.yolo"]):
        try:
            ast.parse(Path(mod.__file__).read_text(encoding="utf-8"), feature_version=(3, 11))
            check(f"3.11 语法: {Path(mod.__file__).name}", True)
        except SyntaxError as exc:
            check(f"3.11 语法: {Path(mod.__file__).name}", False, str(exc))

    print()
    if FAILS:
        print(f"结果：{len(FAILS)} 项失败 -> {FAILS}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
