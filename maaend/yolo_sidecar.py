"""maa_end YOLO sidecar —— 独立系统 Python 进程(非冻结宿主)跑 YOLO。

为什么 sidecar:N.E.K.O 插件子进程是 Nuitka 冻结 exe,matplotlib 被
--nofollow-import-to 主动排除,import 即抛 RuntimeError;ultralytics 导入链
必经 matplotlib → 在插件进程内永远无法加载。系统 Python(3.14)有完整
torch/ultralytics 栈,本脚本在那里跑,插件进程经 stdin/stdout JSON 行协议调用。

协议(每行一个 JSON):
  请求: {"cmd": "detect", "hwnd": 123}   | {"cmd": "ping"}
        {"cmd": "ocr", "hwnd": 123, "roi": [x1,y1,x2,y2]}   # roi 可选,0-1 比例
  响应: {"ok": true, ...结果}            | {"ok": false, "error": "..."}
启动参数: argv[1]=模型路径 argv[2]=置信度阈值
生命周期: 随 stdin EOF 或父进程退出而终;模型懒加载(首检数秒)。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ---------------------------------------------------------------- Win32 截图

# 截图统一走 maaend/vision/capture.py（2026-11-04 重构：三份重复实现合并）。
# 本脚本由主进程以 `python <path>/yolo_sidecar.py <model> <conf>` 直接启动，
# 没有包上下文，所以把 maaend/ 目录挂进 sys.path，按顶层包导入。
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from vision.capture import capture  # noqa: E402, I001


# ---------------------------------------------------------------- OCR

_OCR = None


def _ensure_ocr():
    global _OCR
    if _OCR is not None:
        return
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:  # 新版包名
        from rapidocr import RapidOCR
    _OCR = RapidOCR()


def ocr_frame(w: int, h: int, bgr: bytes, roi=None) -> dict:
    """对截图跑 OCR。roi=[x1,y1,x2,y2] 为 0-1 比例(分辨率无关),可省略。"""
    import numpy as np

    _ensure_ocr()
    frame = np.frombuffer(bgr, dtype=np.uint8).reshape(h, w, 3)
    if roi and len(roi) == 4:
        x1, y1, x2, y2 = (max(0, min(1, float(v))) for v in roi)
        if x2 > x1 and y2 > y1:
            frame = frame[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]
    result, _ = _OCR(frame)
    lines = []
    for item in result or []:
        try:
            if isinstance(item, dict):  # rapidocr v2+ 形态
                box, text, score = item.get("box"), item.get("txt") or item.get("text"), item.get("score")
            else:  # 经典形态 [box, text, score]
                box, text, score = item[0], item[1], item[2]
            lines.append({"text": str(text), "score": round(float(score), 3),
                          "box": [[round(float(p), 1) for p in pt] for pt in box]})
        except Exception:  # noqa: BLE001 - 单行解析失败不拖垮整次 OCR
            continue
    lines.sort(key=lambda ln: -ln["score"])
    return {
        "ok": True,
        "size": [w, h],
        "lines": lines[:80],
        "full_text": " ".join(ln["text"] for ln in lines),
    }


# ---------------------------------------------------------------- 主循环

_MODEL = None
_CONF = 0.4
_VERSION_INFO: dict = {}


def _ensure_model(path: str):
    global _MODEL
    if _MODEL is not None:
        return
    import ultralytics
    from ultralytics import YOLO  # 系统 Python 里完整可用

    _MODEL = YOLO(path)
    # 跨 ultralytics 版本加载 .pt 可能静默出垃圾置信度 → 把两边版本带回去,不一致时给警告
    ckpt = getattr(_MODEL, "ckpt", None)
    saved = ckpt.get("version") if isinstance(ckpt, dict) else None
    _VERSION_INFO["ultralytics"] = ultralytics.__version__
    _VERSION_INFO["model_saved_with"] = saved
    if saved and saved != ultralytics.__version__:
        _VERSION_INFO["version_warning"] = (
            f"模型由 ultralytics {saved} 保存,当前运行 {ultralytics.__version__};"
            "若检测置信度异常偏低,请换同版本 ultralytics"
        )


def handle(req: dict) -> dict:
    cmd = str(req.get("cmd") or "")
    if cmd == "ping":
        return {"ok": True, "pong": True}
    if cmd == "ocr":
        hwnd = int(req.get("hwnd") or 0)
        if not hwnd:
            return {"ok": False, "error": "hwnd 为空"}
        w, h, bgr = capture(hwnd)
        roi = req.get("roi") if isinstance(req.get("roi"), list) else None
        return ocr_frame(w, h, bgr, roi)
    if cmd != "detect":
        return {"ok": False, "error": "未知 cmd: " + cmd}
    hwnd = int(req.get("hwnd") or 0)
    if not hwnd:
        return {"ok": False, "error": "hwnd 为空"}
    w, h, bgr = capture(hwnd)
    _ensure_model(str(req.get("model") or ""))
    import numpy as np

    frame = np.frombuffer(bgr, dtype=np.uint8).reshape(h, w, 3)
    results = _MODEL.predict(frame, conf=_CONF, verbose=False)
    objects = []
    for r in results:
        names = r.names
        for box in r.boxes:
            cls_id = int(box.cls.item())
            objects.append(
                {
                    "label": str(names.get(cls_id, cls_id)),
                    "confidence": round(float(box.conf.item()), 3),
                    "box": [round(float(v), 1) for v in box.xyxy[0].tolist()],
                }
            )
    objects.sort(key=lambda d: d["confidence"], reverse=True)
    return {"ok": True, "size": [w, h], "count": len(objects), "objects": objects[:30],
            **_VERSION_INFO}


def main() -> None:
    global _CONF
    model_path = sys.argv[1] if len(sys.argv) > 1 else ""
    if len(sys.argv) > 2:
        _CONF = float(sys.argv[2])
    if not model_path or not Path(model_path).is_file():
        print(json.dumps({"ok": False, "error": "模型路径无效: " + model_path}), flush=True)
        return
    print(json.dumps({"ok": True, "ready": True, "model": model_path}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            req.setdefault("model", model_path)
            resp = handle(req)
        except Exception as exc:  # noqa: BLE001 - 单次失败不退出服务
            resp = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:200]}
        print(json.dumps(resp, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
