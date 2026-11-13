"""画面场景判别：判断当前游戏窗口停在哪一屏。

模型：YOLO26n-cls 33 类（scene_y26_33c），在终末地实机截图上微调。
⚠️ 训练集仍在迭代、指标是暂态的：本文不写死准确率数字，一律以最新评估记录为准。

选型记录（更早版本、同数据同超参对比）：曾与 YOLO11n-cls 对比，组级准确率接近
（94.6% vs 93.6%，差异 5 帧，不算显著），但 YOLO11n 把世界场景误判成「剧情对话」
的有 34/319（11%），YOLO26n 只有 6/319（2%）—— 这个 9 个百分点的差距是实质
差异，是选 YOLO26n 的理由。
多代权重都留在 data/scene/ 下（scene_y26_33c / scene_y11_30c 等），改 scene_model
即可切回。

类别名不写死在这里——实际类别表从随模型附带的 `*.names.json` 读，
换权重的只需同步换 json，不用改代码。下列 CLASSES 只是 json 缺失时的兜底。

⚠️ 预处理必须与训练完全一致，否则准确率会掉到 16%：
ultralytics 训练分类模型时 `mean=None,std=None` → 只做 ToTensor（缩放到 0~1），
**不做 ImageNet 归一化**（见 ultralytics/data/augment.py 的 DEFAULT_MEAN/STD）。
几何部分是「短边缩放到 224 + 中心裁剪」，不是把画面硬压成 224x224。
改这里的预处理之前，先用 classify_transforms 对比验证。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

#: 类别名兜底（33 类，顺序即训练时的类别下标 0 起）。
#: 正常路径是从 *.names.json 读；这里只在 json 缺失时兜底。
CLASSES: tuple[str, ...] = (
    "分解界面", "剧情对话", "加载LOGO", "图鉴查询", "地图-平面图", "地图-雷达式",
    "基建俯视", "基建菜单", "好友-社交界面", "室内-仓库", "室内-工业", "室内-广场",
    "室内-楼梯", "室内-走廊", "室外-土路", "室外-高空平台", "战利品弹窗", "战斗",
    "据点交互界面", "武器-物品网格", "流程节点图", "确认弹窗", "简易合成",
    "终端-代码界面", "装备改造", "角色列表", "角色详情", "设备制造", "设置菜单",
    "说明-图鉴面板", "过场黑屏", "野外-草地", "野外-荒地石板",
)

#: 「玩家正在场景里走动/对话」而非操作界面的类。其余都是 UI 屏，可以点菜单。
#: 不能写成"UI_CLASSES = 全部 - 某几个"，因为换权重后类别集会变，
#: 那样算出来的 UI 集合会悄悄错位。
SCENE_CLASSES: frozenset[str] = frozenset({
    "室内-仓库", "室内-工业", "室内-广场", "室内-楼梯", "室内-走廊",
    "室外-土路", "室外-高空平台", "战斗", "野外-草地", "野外-荒地石板",
    "剧情对话",
    # 兼容 17 类模型（插件里两套权重都留着，配置项 scene_model 切换）
    "游戏内场景",
})

IMGSZ = 224

#: 数据目录候选。插件被并进 game_suite 后，data/ 的真实位置有两个可能：
#:   1) <game_suite>/maa_end/data/scene   —— 插件自带 data/ 时的位置
#:   2) <plugins>/maa_end/data/scene      —— SDK 的 data_path() 指向的位置
#: 写死任何一个都会在另一种布局下 exists=False，所以按候选逐个探测。
#: router 里会用 self.data_path("scene") 显式传路径，这里的默认值只兜底。
_HERE = Path(__file__).resolve().parent
DATA_DIR_CANDIDATES = (
    _HERE.parent / "data" / "scene",
    _HERE.parent.parent.parent / "maa_end" / "data" / "scene",
)
DATA_DIR = next((p for p in DATA_DIR_CANDIDATES if p.is_dir()), DATA_DIR_CANDIDATES[0])
DEFAULT_MODEL = DATA_DIR / "scene_y26_33c.onnx"
DEFAULT_NAMES = DATA_DIR / "scene_y26_33c.names.json"


class SceneError(RuntimeError):
    """模型缺失或推理后端不可用。"""


def _preprocess(img) -> Any:
    """与 ultralytics 训练时完全一致：短边缩放 → 中心裁剪 → ToTensor → 不归一化。

    需要 numpy / PIL；两者都是 onnxruntime 与采集链路的既有依赖。
    """
    import numpy as np
    from PIL import Image

    if not isinstance(img, Image.Image):
        img = Image.fromarray(np.asarray(img))
    img = img.convert("RGB")
    w, h = img.size
    scale = IMGSZ / min(w, h)
    img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.BILINEAR)
    w, h = img.size
    left, top = (w - IMGSZ) // 2, (h - IMGSZ) // 2
    img = img.crop((left, top, left + IMGSZ, top + IMGSZ))
    return np.asarray(img, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0


class SceneClassifier:
    """ONNX 场景判别器。首次调用加载会话，之后复用（构造本身不做 IO）。"""

    def __init__(self, model_path: str | Path = "", names_path: str | Path = "") -> None:
        self.model_path = Path(model_path) if model_path else DEFAULT_MODEL
        self.names_path = Path(names_path) if names_path else DEFAULT_NAMES
        self._sess = None
        self._names: tuple[str, ...] = ()
        self._input = ""
        self._load_ms = 0

    # ---------------------------------------------------------------- 加载
    def _ensure(self) -> None:
        if self._sess is not None:
            return
        if not self.model_path.is_file():
            raise SceneError(f"场景模型不存在：{self.model_path}")
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - 环境缺依赖
            raise SceneError("缺 onnxruntime，无法加载场景模型") from exc

        t0 = time.perf_counter()
        self._sess = ort.InferenceSession(str(self.model_path),
                                          providers=["CPUExecutionProvider"])
        self._input = self._sess.get_inputs()[0].name
        self._load_ms = (time.perf_counter() - t0) * 1000
        self._names = self._load_names()

    def _load_names(self) -> tuple[str, ...]:
        """类别名优先读随模型附带的 json（避免和权重版本脱节）。"""
        if self.names_path.is_file():
            try:
                data = json.loads(self.names_path.read_text(encoding="utf-8"))
                names = data.get("classes") if isinstance(data, dict) else data
                if isinstance(names, list) and names:
                    return tuple(str(n) for n in names)
            except (OSError, json.JSONDecodeError):
                pass
        return CLASSES

    # ---------------------------------------------------------------- 推理
    def classify(self, img, topk: int = 3) -> dict[str, Any]:
        """判别一张画面。img 接受 PIL.Image 或 numpy 数组。"""
        import numpy as np

        self._ensure()
        x = _preprocess(img)
        t0 = time.perf_counter()
        logits = self._sess.run(None, {self._input: x})[0][0]
        dt = (time.perf_counter() - t0) * 1000

        e = np.exp(logits - logits.max())
        probs = e / e.sum()
        order = probs.argsort()[::-1]
        k = max(1, min(int(topk), len(self._names)))
        items = [{"class": self._names[int(i)], "confidence": float(probs[int(i)])}
                 for i in order[:k]]
        top = items[0]
        return {
            "found": True,
            "class": top["class"],
            "confidence": top["confidence"],
            "is_ui": self.is_ui(top["class"]),
            "topk": items,
            "infer_ms": round(dt, 1),
            "model": str(self.model_path),
        }

    def probe(self) -> dict[str, Any]:
        """探测模型是否就绪（不推理）。"""
        info: dict[str, Any] = {
            "model": str(self.model_path),
            "exists": self.model_path.is_file(),
        }
        if not info["exists"]:
            info["classes"] = len(CLASSES)
            return info
        try:
            self._ensure()
            info["ready"] = True
            info["load_ms"] = round(self._load_ms, 1)
            info["classes"] = len(self._names)
            info["class_names"] = list(self._names)
        except SceneError as exc:
            info["ready"] = False
            info["error"] = str(exc)
        return info

    def is_ui(self, class_name: str) -> bool:
        """该类别是否属于"可操作界面"。默认按 SCENE_CLASSES 取反，
        类别表里出现未知类名时按 UI 处理（宁可多看一眼菜单，不要在场景里乱点）。"""
        return class_name not in SCENE_CLASSES
