"""领域层 —— 纯逻辑，零 IO，可脱离 SDK / Win32 单测。

分层规则（docs/REFACTOR-PLAN.md §3.2）：`domain/` 不许 import `api/`、`vision/`、
`backends/`；不许碰网络与文件系统。这样这些模块的测试永远是秒级、可离线跑。

目前只有 `events.py`；后续迁入的 `assist_rules` / `executor` / `remind` / `endaxis`
仍留在 `maaend/` 根（阶段 4 未完成部分）。
"""
