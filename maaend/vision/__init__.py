"""视觉层：截图、帧转换、检测/OCR 客户端与子进程 worker。

分层规则（docs/REFACTOR-PLAN.md §3.2）：
- 本包是**全插件唯一的 Win32 截图实现**，任何模块需要画面都从这里拿；
- `workers/` 里的脚本是独立系统 Python 进程，不 import 本包以外的插件代码；
- 客户端（yolo_client / ocr_client）只负责进程管理与协议编解码，不做业务判断。
"""
