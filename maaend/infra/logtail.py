"""增量读取追加型日志文件 —— 全插件唯一实现。

合并历史（docs/REFACTOR-PLAN.md 阶段 5 #2）：
这段逻辑原本有两份，骨架相同但语义分叉：

| 维度 | `assist.ZoneReader._read_file_new` | `stream._LogTailer` |
|---|---|---|
| 首次读取 | 只回看尾部 `LOG_TAIL_BYTES` | 从头读 |
| 半行 | 回退到最后一个 `\\n`，剩余下轮读 | 不处理，可能读到半行 |
| 状态 | `dict[path → offset]`（多文件轮询） | 每实例一个文件 |
| 产出 | 解析后的事件 | 原始文本行 |

两份都在解「文件追加了，我该从哪读到哪」这同一个问题，只是消费方式不同。
这里用参数保住两种语义，状态仍由调用方持有 —— 不把状态机也搬过来，
避免为了"统一"去改实时辅助的轮询结构。

**关于 `partial_line`**：写日志的进程可能在任意时刻 flush，读到半行是常态。
`True`（默认）表示宁可这轮少读一点，也不把半行交给解析器 ——
解析器遇到半行只会静默丢掉，等于这条事件永久丢失。`False` 只适合
「读到的内容本来就不是逐行协议」的场景。
"""

from __future__ import annotations

from pathlib import Path


def read_new_bytes(
    path: Path,
    offset: int,
    *,
    tail_bytes: int = 0,
    partial_line: bool = True,
) -> tuple[bytes, int]:
    """从 `offset` 往后读新增内容，返回 `(数据, 新偏移)`。

    参数：
    - `offset`：上次读到的字节位置。**首次读取传 -1**（负数即"没见过这个文件"），
      由 `tail_bytes` 决定回看多少：`0` = 从头读，`>0` = 只回看尾部 N 字节。
    - `tail_bytes`：仅首次生效。
    - `partial_line`：`True` 时丢弃末尾不完整的行，新偏移停在最后一个 `\\n` 之后。

    返回值恒为二元组；任何 IO 失败都返回 `(b"", offset)`，即"没读到新东西，位置不动"
    —— 调用方下一轮自然会重试，不需要区分错误类型。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return b"", offset
    if offset < 0:  # 首见：按 tail_bytes 决定回看范围
        offset = max(0, size - max(0, int(tail_bytes)))
    if size < offset:  # 轮转 / 被截断：从头重读（上层靠指纹去重防重复入流）
        offset = 0
    if size <= offset:
        return b"", offset
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read(size - offset)
    except OSError:
        return b"", offset
    if not partial_line:
        return chunk, size
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:  # 一个完整行都没有：位置不动，下轮再读
        return b"", offset
    return chunk[:last_nl], offset + last_nl + 1
