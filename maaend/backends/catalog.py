"""interface.json 读取：MaaEnd 发布包里的 MAA 标准任务清单。

这个文件是 JSON5（带 ``//`` 注释和尾逗号，CI 还会往里合并生成内容），标准库
``json`` 不认。这里做最小容错：去行注释、去尾逗号，然后交给 ``json.loads``。
不支持多行字符串等罕见语法——遇到解析失败就把原始错误报给调用方，不静默吞。

契约（来自 assets/interface.json 与 MaaFramework 的 ProjectInterface 协议）：

* ``task`` 是**数组**：``[{name, entry, description, option, ...}, ...]``
* ``controller`` / ``resource`` / ``agent`` 描述控制器、资源与 AgentServer 子进程
* 任务名随发布版本变化——所以本插件从不写死任务名，清单以安装目录的文件为准
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def strip_json5(text: str) -> str:
    """去掉注释与尾逗号（meojson 解析器允许的 JSON5 形态）。

    带字符串感知的状态机：字符串内的 ``//``、``/*`` 不当注释；支持行注释、
    块注释、尾逗号。这是通用实现——MaaEnd 的任务子文件里有各种位置的注释。
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_catalog(path: str | Path, *, _seen: set[str] | None = None) -> dict[str, Any]:
    """读取并解析 interface.json，跟随 ``import`` 引用的子清单。

    MaaEnd 的主 ``interface.json`` 里 ``task`` 常为空数组，真正的任务定义分布在
    ``import`` 引用的文件（相对本文件目录，如 ``tasks/AutoStockpile.json``）。
    解析失败抛 ValueError（带原始原因）。
    """
    path = Path(path)
    seen = _seen if _seen is not None else set()
    key = str(path.resolve()).lower()
    if key in seen:
        return {}
    seen.add(key)

    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        catalog = json.loads(strip_json5(raw))
    except ValueError as exc:
        raise ValueError(f"interface.json 解析失败（{path}）：{exc}") from exc
    if not isinstance(catalog, dict):
        raise ValueError(f"interface.json 顶层不是对象（{path}）")

    imports = catalog.get("import")
    if isinstance(imports, list):
        merged_tasks: list[Any] = list(catalog.get("task") or [])
        merged_groups: list[Any] = list(catalog.get("group") or [])
        merged_options: dict[str, Any] = dict(catalog.get("option") or {})
        for rel in imports:
            if not isinstance(rel, str) or not rel.strip():
                continue
            child_path = path.parent / rel
            try:
                child = load_catalog(child_path, _seen=seen)
            except (ValueError, OSError) as exc:
                # 子清单缺失不让整个目录失效：如实记一条，继续。
                catalog.setdefault("import_errors", []).append(
                    {"file": rel, "error": str(exc)}
                )
                continue
            merged_tasks.extend(child.get("task") or [])
            merged_groups.extend(child.get("group") or [])
            child_opts = child.get("option")
            if isinstance(child_opts, dict):
                merged_options.update(child_opts)
        catalog["task"] = merged_tasks
        catalog["group"] = merged_groups
        catalog["option"] = merged_options
    return catalog


def tasks_of(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """规整任务清单：name / entry / description / options。跳过缺 entry 的残项。"""
    raw = catalog.get("task")
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry = str(item.get("entry") or "").strip()
        if not entry:
            continue
        option_names = [
            str(o) for o in item.get("option") or [] if isinstance(o, str) and o
        ]
        result.append(
            {
                "name": str(item.get("name") or entry),
                "entry": entry,
                "description": str(item.get("description") or ""),
                "option_names": option_names,
            }
        )
    return result


def meta_of(catalog: dict[str, Any]) -> dict[str, Any]:
    """清单级元信息：名称、版本、控制器、资源、agent 子进程。"""
    controllers = catalog.get("controller")
    resources = catalog.get("resource")
    agents = catalog.get("agent")
    return {
        "interface_name": catalog.get("name") or "",
        "interface_version": catalog.get("version") or "",
        "controllers": [
            str(c.get("name")) for c in controllers if isinstance(c, dict) and c.get("name")
        ]
        if isinstance(controllers, list)
        else [],
        "resources": [
            str(r.get("name")) for r in resources if isinstance(r, dict) and r.get("name")
        ]
        if isinstance(resources, list)
        else [],
        "agents": [
            str(a.get("child_exec")) for a in agents if isinstance(a, dict) and a.get("child_exec")
        ]
        if isinstance(agents, list)
        else [],
    }


def options_of(catalog: dict[str, Any]) -> dict[str, Any]:
    """合并后的选项定义映射（MaaEnd 把定义内联在 import 的子清单里）。"""
    opt = catalog.get("option")
    return dict(opt) if isinstance(opt, dict) else {}


def controller_type_of(catalog: dict[str, Any], name: str) -> str:
    """控制器名的 type 字段（如 "Win32" / "Adb"）；未知返回空串。"""
    controllers = catalog.get("controller")
    if not isinstance(controllers, list):
        return ""
    for c in controllers:
        if isinstance(c, dict) and str(c.get("name") or "") == name:
            return str(c.get("type") or "")
    return ""
