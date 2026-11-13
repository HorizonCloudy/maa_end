"""入口层公共件 —— 把 router 里复制粘贴的样板收成一处。

拆分原则（docs/REFACTOR-PLAN.md §3.2）：`api/` 只做参数校验与转调，
不内联 ctypes/subprocess/numpy。这里放的是**纯函数**，不持有状态，
任何 mixin 都能直接用。
"""

from __future__ import annotations

from typing import Any

from plugin.sdk.plugin import Err

from ..infra.probe import find_game_window


def game_window(settings: dict[str, Any]) -> dict[str, Any]:
    """按配置里的类名/标题正则找游戏窗口。

    重构前这段在 router 里重复了 9 次（每次两行 `str(settings.get(...) or "")`），
    改一个键名要改九处。现在只有这里知道「窗口靠哪两个配置键定位」。
    """
    return find_game_window(
        str(settings.get("window_class_regex") or ""),
        str(settings.get("window_title_regex") or ""),
    )


def unwrap_tool_result(result: Any) -> Any:
    """llm_tool 的统一错误包装。

    SDK 入口返回 `Result`；工具层要把它转成 `{"output", "is_error", "error"}`
    给模型看。重构前这段样板在 router 里重复了 10 次。
    """
    if isinstance(result, Err):
        msg = str(getattr(result.error, "message", result.error))
        return {"output": {"error": msg}, "is_error": True, "error": msg[:200]}
    return result.value


def window_or_error(settings: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """取窗口；找不到时给出可直接回给调用方的错误包。

    返回 `(win, None)` 或 `(None, {"error": ...})` —— 调用方 `if err: return err`。
    """
    win = game_window(settings)
    if not win.get("found"):
        return None, {"error": "游戏窗口未找到（检查 window_class_regex / window_title_regex）"}
    return win, None
