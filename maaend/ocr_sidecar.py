"""maa_end OCR sidecar —— 独立系统 Python 进程跑 RapidOCR(画面文字识别)。

为什么 sidecar:同 yolo_sidecar.py。插件宿主是 Nuitka 冻结 exe,rapidocr 的
导入链(onnxruntime/shapely/cv2)在插件进程内不可靠;系统 Python(3.14)里
rapidocr_onnxruntime 实测可 import。

协议(每行一个 JSON,请求/响应):
  {"cmd": "ping"}
  {"cmd": "ocr", "hwnd": 402062}                 全画面文字行(带本体归类)
  {"cmd": "find", "hwnd": 402062, "text": "活动"} 找词 → 客户区/屏幕坐标(可点)
  {"cmd": "ocr_image", "path": "reward.png"}     离线图(奖励截图/采集帧)
响应: {"ok": true, ...} | {"ok": false, "error": "..."}

启动参数: argv[1]=最小置信度(默认 0.55)  argv[2]=本体 json 路径(可选)
生命周期: stdin EOF 或父进程退出即终;引擎懒加载(首帧数秒)。
本体命中时按 auto_label.py 的 KIND_TO_CLASS 归类,类名与 YOLO 9 类同名
(enemy_name/operator_name/item_name),上层可"看到敌人框 + 读到敌人名"。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # 被当模块导入时也找得到同目录的 yolo_sidecar
    sys.path.insert(0, str(HERE))

# 与 data/yolo/auto_label.py 保持一致(名字本体 → 标注类)
KIND_TO_CLASS = {
    "enemy": "enemy_name", "operator": "operator_name",
    "item": "item_name", "weapon": "item_name", "gear": "item_name",
    "facility": "item_name",
}

_ENGINE = None
_MIN_CONF = 0.55
_INDEX: dict[str, str] = {}   # name → class
_NAMES: list[str] = []         # 按长度倒排(长名优先,避免"天使"抢"大角天使")


# ---------------------------------------------------------------- 本体

def load_ontology_dict(onto: dict) -> int:
    """从已加载的本体 dict 建 name→class 索引;返回条数。"""
    global _INDEX, _NAMES
    idx: dict[str, str] = {}
    for name, info in (onto.get("name_index") or {}).items():
        cls = KIND_TO_CLASS.get(str((info or {}).get("kind") or ""))
        if cls and 2 <= len(name) <= 12:
            idx[name] = cls
    _INDEX = idx
    _NAMES = sorted(idx, key=len, reverse=True)
    return len(idx)


def load_ontology(path: str) -> int:
    """加载 wiki_ontology.json 的 name_index;失败则清空索引返回 0。"""
    try:
        onto = (json.loads(Path(path).read_text(encoding="utf-8"))
                if path and Path(path).is_file() else {})
    except (OSError, ValueError):
        onto = {}
    return load_ontology_dict(onto)


def _match(text: str) -> tuple[str, str] | None:
    """文本 → (本体名字, 类)。规则与 auto_label.OcrLabeler._match 一致。"""
    if text in _INDEX:
        return text, _INDEX[text]
    if len(text) >= 4:
        for name in _NAMES:
            if abs(len(name) - len(text)) <= 1 and name[0] == text[0]:
                if len(name) == len(text):
                    if sum(1 for a, b in zip(name, text) if a != b) <= 1:
                        return name, _INDEX[name]
                elif len(name) == len(text) + 1 and name[1:] == text[1:] or \
                        len(name) == len(text) - 1 and name == text[:-1]:
                    return name, _INDEX[name]
    for name in _NAMES:  # 完整名字出现在文本里(如 "潜地虬兽 ×3")
        if len(name) >= 3 and name in text:
            return name, _INDEX[name]
    return None


# ---------------------------------------------------------------- OCR

def _ensure_engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR  # 系统 Python 里完整可用

        _ENGINE = RapidOCR()
    return _ENGINE


def _origin(hwnd: int) -> tuple[int, int]:
    """客户区原点对应的屏幕坐标(找词要能直接点)。"""
    user = ctypes.WinDLL("user32")
    pt = wt.POINT(0, 0)
    if not user.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(pt)):
        return (0, 0)
    return (int(pt.x), int(pt.y))


def _load_image(path: str):
    """读图:Windows 下 cv2.imread 不吃非 ASCII 路径,走 fromfile+imdecode。"""
    import cv2
    import numpy as np

    data = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


def _lines(img) -> list[dict]:
    import numpy as np  # rapidocr 自带依赖

    frame = img if isinstance(img, np.ndarray) else np.asarray(img)
    result, _ = _ensure_engine()(frame)
    out: list[dict] = []
    for box, text, score in result or []:
        t = str(text).strip().replace(" ", "")
        sc = float(score)
        if len(t) < 2 or sc < _MIN_CONF:
            continue
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        b = [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]
        cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
        hit = _match(t)
        out.append({
            "text": t,
            "conf": round(sc, 3),
            "box": b,
            "center": [cx, cy],
            "tag": hit[0] if hit else "",
            "cls": hit[1] if hit else "",
        })
    out.sort(key=lambda d: (d["cls"] == "", -d["conf"]))  # 本体命中的排前面
    return out


def _from_hwnd(hwnd: int, roi: list | None = None,
               scale: float = 1.0) -> tuple[list[dict], list[int], list[int]]:
    """抓帧 → 可选 ROI 裁剪/降采样 → OCR;坐标映射回全客户区。"""
    # 延迟导入：HERE 要先加进 sys.path 才找得到 vision 包。
    # 放在函数内而非模块顶层，避免 ruff E402（CI 用 --ignore-noqa）。
    from vision.capture import capture  # 截图唯一实现:vision/capture.py

    w, h, bgr = capture(hwnd)
    import numpy as np

    frame = np.frombuffer(bgr, dtype=np.uint8).reshape(h, w, 3)
    ox, oy = _origin(hwnd)
    rx0, ry0 = 0, 0
    if roi and len(roi) == 4:
        rx0, ry0 = max(0, int(roi[0])), max(0, int(roi[1]))
        rx1, ry1 = min(w, int(roi[2])), min(h, int(roi[3]))
        if rx1 - rx0 > 8 and ry1 - ry0 > 8:  # 合法 ROI 才裁
            frame = frame[ry0:ry1, rx0:rx1]
        else:
            rx0, ry0 = 0, 0
    scale = float(scale or 1.0)
    if 0.3 <= scale < 1.0:  # 降采样提速(坐标再除回去)
        import cv2
        frame = cv2.resize(frame, (int(frame.shape[1] * scale),
                                   int(frame.shape[0] * scale)))
    lines = _lines(frame)
    inv = 1.0 / scale
    for ln in lines:  # ROI/缩放映射回全客户区坐标
        ln["box"] = [round(v * inv) for v in ln["box"]]
        ln["box"][0] += rx0
        ln["box"][1] += ry0
        ln["box"][2] += rx0
        ln["box"][3] += ry0
        cx, cy = (ln["box"][0] + ln["box"][2]) // 2, (ln["box"][1] + ln["box"][3]) // 2
        ln["center"] = [cx, cy]
        ln["screen"] = [cx + ox, cy + oy]
    return lines, [w, h], [ox, oy]


# ---------------------------------------------------------------- 主循环

def handle(req: dict) -> dict:
    cmd = str(req.get("cmd") or "")
    if cmd == "ping":
        return {"ok": True, "pong": True, "index": len(_INDEX),
                "min_conf": _MIN_CONF, "engine": "rapidocr_onnxruntime"}
    if cmd == "ocr":
        hwnd = int(req.get("hwnd") or 0)
        if not hwnd:
            return {"ok": False, "error": "hwnd 为空"}
        roi = req.get("roi") or None
        scale = float(req.get("scale") or 1.0)
        lines, size, origin = _from_hwnd(hwnd, roi, scale)
        return {"ok": True, "size": size, "origin": origin,
                "count": len(lines), "lines": lines[:60]}
    if cmd == "find":
        hwnd = int(req.get("hwnd") or 0)
        needle = str(req.get("text") or "").strip()
        if not hwnd:
            return {"ok": False, "error": "hwnd 为空"}
        if not needle:
            return {"ok": False, "error": "text 为空"}
        roi = req.get("roi") or None
        scale = float(req.get("scale") or 1.0)
        lines, size, origin = _from_hwnd(hwnd, roi, scale)
        low = needle.replace(" ", "").lower()
        hits = [ln for ln in lines if low in ln["text"].lower() or low in ln["tag"].lower()]
        best = None
        if hits:  # 完全相等 > 本体命中 > 包含(统一按归一化文本比,带空格的输入也能命中)
            best = next((ln for ln in hits if ln["text"].lower() == low), None) or \
                next((ln for ln in hits if ln["tag"] and ln["tag"].lower() == low), None) \
                or hits[0]
        return {"ok": True, "found": bool(hits), "text": needle,
                "size": size, "origin": origin, "count": len(hits),
                "match": best}
    if cmd == "ocr_image":
        path = str(req.get("path") or "")
        if not path:
            return {"ok": False, "error": "path 为空"}
        img = _load_image(path)
        if img is None:
            return {"ok": False, "error": "图片读不出: " + path[:120]}
        lines = _lines(img)
        return {"ok": True, "size": [int(img.shape[1]), int(img.shape[0])],
                "count": len(lines), "lines": lines[:60]}
    return {"ok": False, "error": "未知 cmd: " + cmd}


def main() -> None:
    global _MIN_CONF
    if len(sys.argv) > 1:
        try:
            _MIN_CONF = float(sys.argv[1])
        except ValueError:
            pass
    onto = sys.argv[2] if len(sys.argv) > 2 else ""
    n = load_ontology(onto)
    print(json.dumps({"ok": True, "ready": True, "engine": "rapidocr_onnxruntime",
                      "index": n, "min_conf": _MIN_CONF,
                      "ontology": onto}, ensure_ascii=False), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line))
        except Exception as exc:  # noqa: BLE001 - 单次失败不退出服务
            resp = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:200]}
        print(json.dumps(resp, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
