"""YOLO / OCR / 场景判别 / 数据流。

由 `maaend/router.py` 拆分而来（2026-11-04，见 docs/REFACTOR-PLAN.md 阶段 3）。
状态一律经 `self.state`（AppState）读写，不直接持有实例字段。
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from plugin.sdk.plugin import (
    Err,
    Ok,
    SdkError,
    llm_tool,
    plugin_entry,
)

from ._helpers import game_window


class VisionAPI:
    """YOLO / OCR / 场景判别 / 数据流。"""

    @plugin_entry(
        id="ef_ocr",
        name="读屏 OCR",
        description="对终末地窗口截图跑 OCR（rapidocr，视觉 sidecar 执行，会把游戏带到前台）："
        "返回 full_text 与逐行 lines（文本/置信度/框）。roi 可选 [x1,y1,x2,y2]"
        "（0-1 比例，JSON 数组）缩小范围提速。问「现在理智多少」「屏幕上写了什么」用它。",
        llm_result_fields=["summary", "full_text", "lines"],
        timeout=60.0,
    )
    async def ocr(
        self,
        roi: Annotated[str, "ROI [x1,y1,x2,y2]（0-1 比例）JSON 数组；空=全屏"] = "",
        **_,
    ):
        detector = self.state.detector
        if detector is None:
            return Err(SdkError("视觉 sidecar 未启动（检查 maa.stream_enabled 与启动日志）。"))
        settings = await self._current_settings()
        win = game_window(settings)
        if not win.get("found"):
            return Err(SdkError("游戏窗口不在线：先启动《明日方舟：终末地》。"))
        roi_list = None
        text = (roi or "").strip()
        if text:
            try:
                roi_list = json.loads(text)
            except ValueError as exc:
                return Err(SdkError(f"roi 不是合法 JSON：{exc}"))
            if not (isinstance(roi_list, list) and len(roi_list) == 4
                    and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in roi_list)):
                return Err(SdkError("roi 必须是 4 个 0-1 数字的数组 [x1,y1,x2,y2]。"))
        try:
            resp = await asyncio.to_thread(detector.ocr_hwnd, int(win["hwnd"]), roi_list)
        except Exception as exc:  # noqa: BLE001 - 原始错误透出便于定位
            return Err(SdkError(f"{type(exc).__name__}: {exc}"))
        if not resp.get("ok"):
            return Err(SdkError(str(resp.get("error") or "sidecar 拒绝")))
        lines = resp.get("lines") or []
        resp["window"] = win.get("title") or ""
        resp["summary"] = f"读到 {len(lines)} 行：{(resp.get('full_text') or '')[:80]}"
        return Ok(resp)

    @plugin_entry(
        id="ef_yolo",
        name="YOLO 视觉检测",
        description="立即对终末地窗口截图跑一次 YOLO 目标检测（BetterGI 同款视觉路线；"
        "模型放 data/yolo/，默认随插件附带的终末地 9 类模型 endfield_yolo_v2.pt，可换自训模型）。"
        "跨插件可调：套组决策/陪伴链路用它看画面里有什么。返回 objects[{label,confidence,box}]。",
        llm_result_fields=["summary", "count", "objects"],
        timeout=60.0,
    )
    async def yolo_detect(self, **_):
        if self.state.stream is None:
            return Err(SdkError("数据流未启动（检查 maa.stream_enabled），YOLO 检测不可用。"))
        try:
            result = await asyncio.to_thread(self.state.stream.detect_now)
        except Exception as exc:  # noqa: BLE001 - 原始错误透出便于定位
            return Err(SdkError(f"{type(exc).__name__}: {exc}"))
        if isinstance(result, dict) and result.get("error"):
            return Err(SdkError(str(result["error"])))
        objects = result.get("objects") or []
        result["summary"] = (
            f"检测到 {result.get('count', len(objects))} 个目标："
            + "、".join(f"{o.get('label')}({o.get('confidence')})" for o in objects[:6])
        )
        return Ok(result)

    @plugin_entry(
        id="ef_stream",
        name="数据流读取",
        description="读取本插件数据流（tasks 任务事件 / window 窗口状态 / yolo 检测）。"
        "since 传上次返回的 latest_id 即增量拉取；跨插件可调（陪伴/决策链路用）。",
        llm_result_fields=["count", "latest_id"],
        timeout=30.0,
    )
    async def stream_read(
        self,
        category: Annotated[str, "tasks / window / yolo；空=全部"] = "",
        since: Annotated[int, "上次读到的最大 id（增量游标）"] = 0,
        limit: Annotated[int, "最多返回条数（默认 50）"] = 50,
        **_,
    ):
        if self.state.stream is None:
            return Err(SdkError("数据流未启动（检查 maa.stream_enabled）。"))
        cat = category if category in ("tasks", "window", "yolo") else ""
        try:
            entries = self.state.stream.store.read(
                category=cat, since=max(0, int(since or 0)),
                limit=max(1, min(int(limit or 50), 100)))
        except (TypeError, ValueError):
            return Err(SdkError("since/limit 必须是整数。"))
        return Ok({
            "entries": entries,
            "latest_id": self.state.stream.store.latest_id(),
            "count": len(entries),
        })

    async def _ocr_precheck(self) -> tuple[dict[str, Any], Any, int]:
        """OCR 调用前的公共前置:开关 → 游戏窗口 → 客户端。失败返回 (错误包, None, 0)。"""
        settings = await self._current_settings()
        if not bool(settings.get("ocr_enabled", True)):
            return ({"error": "OCR 已关闭(maa.ocr_enabled=false)"}, None, 0)
        win = game_window(settings)
        if not win.get("found"):
            return ({"error": "游戏窗口未找到(检查 window_class_regex/title_regex)"}, None, 0)
        return ({}, self._ocr_client(settings), int(win["hwnd"]))

    @llm_tool(name="maa_ocr_read", description="对游戏窗口截图跑一次 OCR,返回画面上所有文字行(只读,不点击不发送按键)。参数 limit 最多返回行数(默认 40,上限 60)。每行 {text, conf, box[x1,y1,x2,y2], center, screen(屏幕坐标), tag(命中的 wiki 名字), cls(enemy_name/operator_name/item_name,未命中为空)}。用途:确认当前在哪个界面、读关卡名/数量/奖励文字。首次调用会拉起 OCR sidecar(引擎懒加载,数秒)。",
              parameters={"type": "object", "properties": {"limit": {"type": "integer", "description": "最多返回行数,默认 40,上限 60"}}},
              timeout=60.0)
    async def maa_ocr_read_tool(self, **kwargs):
        err, ocr, hwnd = await self._ocr_precheck()
        if err:
            return {"output": err, "is_error": True}
        try:
            limit = max(1, min(int(kwargs.get("limit") or 40), 60))
        except (TypeError, ValueError):
            limit = 40
        import traceback as _tb
        roi = kwargs.get("roi") or None
        try:
            scale = float(kwargs.get("scale") or 1.0)
        except (TypeError, ValueError):
            scale = 1.0
        try:
            res = await asyncio.to_thread(ocr.read_hwnd, hwnd, roi, scale)
        except Exception as exc:  # noqa: BLE001 - 原始错误透出(sidecar 启动失败等可定位)
            return {
                "output": {
                    "error": type(exc).__name__ + ": " + str(exc)[:200],
                    "traceback_tail": _tb.format_exc().strip().splitlines()[-3:],
                },
                "is_error": True,
            }
        if not res.get("ok"):
            return {"output": res, "is_error": True}
        res["lines"] = (res.get("lines") or [])[:limit]
        res["returned"] = len(res["lines"])
        return res

    @llm_tool(name="maa_ocr_find", description="在游戏画面里找一段文字(OCR),返回是否命中以及命中行的 box/center/screen 屏幕坐标(可直接喂给点击驱动)。参数 text=要找的字(如 活动/关卡/领取/开始行动);min_conf 可选,只接受置信度≥该值的命中(默认 0)。只读:不会点击,是否动手由你决定。",
              parameters={"type": "object", "properties": {"text": {"type": "string", "description": "要找的文字"}, "min_conf": {"type": "number", "description": "最低置信度,默认 0"}}, "required": ["text"]},
              timeout=60.0)
    async def maa_ocr_find_tool(self, **kwargs):
        err, ocr, hwnd = await self._ocr_precheck()
        if err:
            return {"output": err, "is_error": True}
        text = str(kwargs.get("text") or "").strip()
        if not text:
            return {"output": {"error": "text 参数为空"}, "is_error": True}
        try:
            min_conf = float(kwargs.get("min_conf") or 0.0)
        except (TypeError, ValueError):
            min_conf = 0.0
        import traceback as _tb
        roi = kwargs.get("roi") or None
        try:
            scale = float(kwargs.get("scale") or 1.0)
        except (TypeError, ValueError):
            scale = 1.0
        try:
            res = await asyncio.to_thread(ocr.find, hwnd, text, roi, scale)
        except Exception as exc:  # noqa: BLE001 - 原始错误透出(sidecar 启动失败等可定位)
            return {
                "output": {
                    "error": type(exc).__name__ + ": " + str(exc)[:200],
                    "traceback_tail": _tb.format_exc().strip().splitlines()[-3:],
                },
                "is_error": True,
            }
        if not res.get("ok"):
            return {"output": res, "is_error": True}
        match = res.get("match") or None
        if match and float(match.get("conf") or 0.0) < min_conf:
            match, res["found"] = None, False
        res["match"] = match
        return res

    def _scene_classifier(self, settings: dict[str, Any]):
        """按配置拼出分类器。模型缺失/后端不可用时抛 SceneError，由入口转成错误包。"""
        from ..scene import SceneClassifier

        data_dir = self.data_path("scene")
        name = str(settings.get("scene_model") or "scene_y26_33c.onnx")
        # 类别表跟着权重走：scene_clsNN.onnx → scene_clsNN.names.json
        stem = name.rsplit(".", 1)[0]
        names = data_dir / f"{stem}.names.json"
        return SceneClassifier(data_dir / name,
                               names if names.is_file() else data_dir / "scene_y26_33c.names.json")

    async def _scene_now(self, topk: int) -> dict[str, Any]:
        """抓游戏窗口 → 判别场景。共享给两个入口。"""
        import traceback as _tb

        from ..scene import CLASSES, SceneError
        from ..vision.capture import capture_window

        settings = await self._current_settings()
        if not bool(settings.get("scene_enabled", True)):
            return {"error": "场景判别已关闭(maa.scene_enabled=false)"}
        win = game_window(settings)
        if not win.get("found"):
            return {"error": "游戏窗口未找到(检查 window_class_regex/title_regex)"}
        hwnd = int(win["hwnd"])

        try:
            clf = self._scene_classifier(settings)
        except SceneError as exc:
            return {"error": str(exc)}
        try:
            width, height, bgr = await asyncio.to_thread(capture_window, hwnd)
            from PIL import Image

            from ..vision.frames import to_rgb

            # capture_window 返回的是 BGR 原始字节，不是数组（见 vision/capture.py）
            result = await asyncio.to_thread(
                clf.classify, Image.fromarray(to_rgb(width, height, bgr)), topk)
        except SceneError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 原始错误透出(抓屏/推理失败可定位)
            return {
                "error": type(exc).__name__ + ": " + str(exc)[:200],
                "traceback_tail": _tb.format_exc().strip().splitlines()[-3:],
            }

        result["window"] = {"hwnd": hwnd, "size": [width, height]}
        result["all_classes"] = list(CLASSES)
        return result

    @llm_tool(name="maa_scene", description="判断当前游戏画面停在哪一屏(截图+分类,只读不点击)。无需参数;topk 可选(默认 3)。返回 class(类别名)、confidence、is_ui(是不是可操作界面)、topk[{class,confidence}]、infer_ms、window。33 类:室内/室外/野外场景、战斗、剧情对话、地图(平面图/雷达式)、基建(俯视/菜单)、制造/合成/装备改造、角色/武器/图鉴、好友、据点、流程节点、终端、设置、确认/战利品弹窗、过场黑屏等(完整类别表随模型 names.json;训练集仍在迭代,准确率以最新评估为准)。用途:动手前先确认现在是不是在菜单里(别在野外乱点)。置信度普遍偏低(十几%属正常),判断以 class 为准别拿 confidence 当阈值。",
              parameters={"type": "object", "properties": {"topk": {"type": "integer", "description": "返回前 k 类,默认 3"}}},
              timeout=60.0)
    async def maa_scene_tool(self, **kwargs):
        from ..scene import CLASSES  # 兜底类别表：topk 上限跟在役类别数走(曾写死 17)
        try:
            topk = max(1, min(int(kwargs.get("topk") or 3), len(CLASSES)))
        except (TypeError, ValueError):
            topk = 3
        res = await self._scene_now(topk)
        if res.get("error"):
            return {"output": res, "is_error": True}
        return res

    @llm_tool(name="maa_scene_expect", description="确认当前画面是否属于期望类别(给自动化做前置检查,只读)。参数 classes 必填,逗号分隔,例如 '基建菜单,确认弹窗' 或 '游戏内场景';topk 可选(默认 3,判定用)。返回 match(true/false)、class、confidence、wanted、topk。match=false 时不要执行依赖该界面的操作。",
              parameters={"type": "object", "properties": {"classes": {"type": "string", "description": "期望类别,逗号分隔"}, "topk": {"type": "integer", "description": "判定用前 k 类,默认 3"}}, "required": ["classes"]},
              timeout=60.0)
    async def maa_scene_expect_tool(self, **kwargs):
        wanted_raw = str(kwargs.get("classes") or "").strip()
        if not wanted_raw:
            return {"output": {"error": "classes 参数为空(例: '基建菜单,确认弹窗')"},
                    "is_error": True}
        wanted = [w.strip() for w in wanted_raw.replace("，", ",").split(",") if w.strip()]
        from ..scene import CLASSES
        try:
            topk = max(1, min(int(kwargs.get("topk") or 3), len(CLASSES)))
        except (TypeError, ValueError):
            topk = 3
        res = await self._scene_now(max(topk, len(wanted)))
        if res.get("error"):
            return {"output": res, "is_error": True}
        hit = res.get("class") in wanted
        names = [t.get("class") for t in (res.get("topk") or [])]
        res["match"] = hit
        res["wanted"] = wanted
        res["matched_in_topk"] = [w for w in wanted if w in names]
        res["summary"] = (
            f"当前是「{res.get('class')}」，"
            + ("属于期望类别。" if hit else f"不在期望类别 {wanted} 内，暂勿执行依赖该界面的操作。")
        )
        return res

    @plugin_entry(
        id="ef_scene_status",
        name="场景模型状态",
        description="场景判别模型是否就绪：模型路径、类别表、加载耗时。排查 ef_scene 报错时先跑这个。",
        llm_result_fields=["summary", "exists", "ready", "classes"],
        timeout=30.0,
    )
    async def scene_status(self, **_):
        settings = await self._current_settings()
        clf = self._scene_classifier(settings)
        info = clf.probe()
        payload: dict[str, Any] = {
            "model": info.get("model", ""),
            "exists": bool(info.get("exists")),
            "ready": bool(info.get("ready")),
            "classes": int(info.get("classes") or 0),
            "load_ms": info.get("load_ms", 0),
            "enabled": bool(settings.get("scene_enabled", True)),
        }
        if not payload["ready"]:
            payload["error"] = info.get("error") or (
                "模型不存在(把 onnx 放到插件 data/scene/ 或改 maa.scene_model)")
            payload["summary"] = "场景模型不可用：" + payload["error"]
        else:
            payload["summary"] = (
                f"场景模型就绪（{payload['classes']} 类，加载 "
                f"{payload['load_ms']} ms，推理约 3-7 ms/张）。")
        return Ok(payload)
