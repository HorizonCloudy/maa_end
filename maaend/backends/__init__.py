"""执行后端（M1–M6）—— 与宿主 / 游戏交互的边界层。

| 模块 | 对应模式 | 职责 |
|---|---|---|
| `control.py` | M1 | MaaEndControl 本地 HTTP/SSE 客户端 |
| `pi_runner.py` | M2 | MaaPiCli 运行目录搭建、PI 配置生成、提权 watcher、进程生命周期 |
| `agent.py` | M3/M6 | 视觉语言代理循环（截图 → VLM 决策 → 键鼠注入） |
| `input_io.py` | M6 | SendInput 注入层（`plan_chunk` 纯函数 + `execute_chunk`） |
| `catalog.py` | — | interface.json（JSON5）解析与 import 递归合并 |
| `maa_fw.py` | — | MaaFramework DLL 版本查询 |
| `gamescaling/` | M6 | **vendor 代码**（THUSI-Lab，独立 LICENSE），保持上游结构不重构 |

分层规则（docs/REFACTOR-PLAN.md §3.2）：本包不 import `api/`；`domain/` 不 import 本包。
`gamescaling/` 的 vendor 属性意味着：改它要同步上游，能不碰就不碰。
"""
