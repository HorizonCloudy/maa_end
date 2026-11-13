# 终末地助手（maa_end）

《明日方舟：终末地》的 N.E.K.O 游戏助手插件：陪玩辅助（陪聊默认开启，卡关自动提醒 + 宏观/精细分层建议）、场景判别（33 类）、YOLO 物品检测、OCR 文本识别、任务清单与 MAA 任务编排、每日任务规划、事件流直读。

> **默认只读。** 识别与辅助层不注入输入、不上传数据；会改动游戏状态的执行类入口由 `allow_run` 闸门控制（**默认关闭**）。战斗事件流（ZMDLogs 子进程）在网卡层抓包，由 `zmdlogs_enabled` 闸门单独控制（**默认关闭**）。

**仅支持 Windows**（截图、窗口枚举、进程核验都走 Win32 API）。

## 能做什么

| 功能 | 说明 | 额外前提 |
|---|---|---|
| 陪玩辅助（`ef_assist`） | 陪聊默认开启；连续失败达到阈值时主动提醒，给宏观/精细建议。只读订阅本地事件流 | 实时判定需要 ZMDLogs 事件流；没有时只读 logs 目录里的历史事件 |
| 场景判别（`maa_scene` 等） | 判断当前停在哪一屏（33 类），模型 `data/scene/scene_y26_33c.onnx`，用宿主自带的 onnxruntime 推理 | 无 |
| YOLO 检测（`ef_yolo`） | 对游戏窗口截图跑物品/敌人/UI 检测，模型 `data/yolo/endfield_yolo_v2.pt` | 外部 Python，能 `import ultralytics` |
| OCR（`ef_ocr`） | 读屏文字，按 `data/yolo/wiki_ontology.json` 归类敌人/干员/物品名 | 外部 Python，能 `import rapidocr_onnxruntime` |
| 任务清单 / 执行（`ef_list_tasks`、`ef_run_task`） | 读 MaaEnd 的 `interface.json` 列任务；放行后经 MaaEnd 自带的 MaaPiCli 执行，可随时 `ef_stop_task` | 安装 MaaEnd；执行需 `allow_run = true` |
| GameScaling 视觉语言代理（`ef_gamescaling`，M6） | VLM 直接游玩：截图发用户自部署的 vLLM 服务，模型回 200ms 键鼠块，SendInput 真实注入；`action=status` 看解析率/延迟 | `gamescaling_enabled = true` + `allow_run = true` + `confirm = true`（三重闸门）；自备 GameScaling checkpoint + vLLM |
| 战斗事件流 / 复盘（`ef_zmdlogs_status`、`ef_report`） | 拉起 ZMDLogs 子进程，订阅 `ws://127.0.0.1:29325/ws` 的伤害/连携/buff 事件；复盘读 session 日志 | ZMDLogs + Npcap + 管理员权限；需 `zmdlogs_enabled = true` |

YOLO 和 OCR 不在宿主进程里跑：宿主是冻结 exe，ultralytics 在里面无法导入，所以插件会用一个外部 Python 起 sidecar 子进程。

## 运行前提

- **MaaEnd**：任务清单/执行功能需要本机装好 MaaEnd（目录里有 `MaaEnd.exe`、`maafw/`、`interface.json`、`resource/`）。`maaend_dir` 留空时自动探测常见位置，找不到会在 `ef_status` 里如实报告。
- **YOLO（可选）**：一个能 `import ultralytics` 的 Python。配置 `yolo_python` 指向它的 `python.exe`；不配则用 PATH 上的 `python`。
- **OCR（可选）**：一个能 `import rapidocr_onnxruntime` 的 Python。配置 `ocr_python`；不配依次退到 `yolo_python`、PATH 上的 `python`。
- **ZMDLogs（可选，默认关）**：需要 Npcap（WinPcap 兼容模式）和管理员权限。ZMDLogs 的源码和 exe 都**不随插件分发**，按配置路径拉起：推荐 `exe` 模式（预构建 `EndfieldLogs.exe`）；`source` 模式还需要上游不提供的 RSA 私钥文件。
- **GameScaling（可选，默认关，M6）**：用户自备模型与服务——从 HuggingFace `THUSI-Lab/GameScaling-*` 下载 checkpoint，用 vLLM 部署（上游仓库 `scripts/serve_vllm.sh`，默认 `http://127.0.0.1:8000`）。插件只做客户端，不打包权重、不拉起服务；推理在 GPU 上跑，显存需求见上游 README（0.8B 单卡 2.2GB 起）。
- **ADB（可选）**：手机/模拟器截图与 ADB 控制器用，默认找 PATH 上的 `adb`。

## 安全闸门与默认值

| 配置 | 默认 | 作用 |
|---|---|---|
| `allow_run` | `false` | 执行总闸门。关闭时 `ef_run_task`、`ef_decide run=true`、排轴 `run`、`maa_daily run`、M3 视觉代理、M5 真实回放全部直接拒绝 |
| `zmdlogs_enabled` | `false` | 抓包总闸门。关闭时不启动任何 ZMDLogs 子进程、不开端口 |
| `control_url` | `""` | M1 控制服务。为空时不会拉起 `MaaEndControl.exe` |
| `ef_replay` 的 `dry_run` | `true` | M5 操作回放默认只校验；真实注入还要 `allow_run` 放行并且 `confirm=true` |
| `ef_agent` 的 `confirm` | `false` | M3 视觉代理接管除 `allow_run` 外还必须显式 `confirm=true` |
| `gamescaling_enabled` | `false` | M6 GameScaling VLM 键鼠接管总闸门；启动还要 `allow_run` 放行 + `ef_gamescaling confirm=true`（三重闸门） |

默认开启的只有只读功能：截图（会把游戏窗口带到前台）、场景判别、YOLO/OCR sidecar、日志轮询、陪玩辅助。

> ⚠ ZMDLogs 会在网卡层抓取并解密游戏流量，可能违反游戏服务条款。是否开启请自行判断。

## 配置

首次运行时宿主把 `config.example.toml` 原样复制到用户数据目录的 `config/plugin.toml`，改那里才生效。完整注释见 `config.example.toml`。常用的键：

| 键 | 默认 | 说明 |
|---|---|---|
| `maaend_dir` | `""` | MaaEnd 安装根目录，留空自动探测 |
| `verify_game` | `"auto"` | `auto` = 检测到终末地安装（鹰角启动器 `games/Arknights Endfield/Endfield.exe`）才核验；`on` / `off` 强制。核验开启时，`run_task` 前要求匹配到的窗口属于已安装的 `Endfield.exe`，防止误绑其他 Unity 游戏 |
| `window_class_regex` / `window_title_regex` | `"UnityWndClass"` / `"Endfield"` | 游戏窗口匹配 |
| `allow_run` | `false` | 见上 |
| `yolo_python` / `ocr_python` | 未设 / `""` | sidecar 用的外部 Python |
| `scene_model` / `yolo_model` | `scene_y26_33c.onnx` / `endfield_yolo_v2.pt` | 换场景模型时在 `data/scene/` 放同名 `.names.json`（YOLO 类别读自 `.pt` 本身） |
| `zmdlogs_enabled` / `zmdlogs_launch_mode` / `zmdlogs_exe` | `false` / `"exe"` / `""` | 见上 |
| `assist_mode` / `consecutive_fail_threshold` | `"宏观建议"` / `3` | 陪玩辅助，面板里也能改 |

## 数据来源与许可

<!-- TODO(用户决定，见 RELEASE-PRECHECK W3/W4)：本节内容待用户确认后补全，不要自行填写。 -->

- **插件许可证：AGPL-3.0**（2026-11-02 拍板）。根目录 `LICENSE` 为完整正文。
- **YOLO 权重**：`endfield_yolo_v2.pt`（v3c）底座为 ultralytics **`yolo26n.pt`**（架构实测与 yolo26n 完全同构），在 4646 张真实游戏帧上做 9 类领域适配后再微调（见 `data/yolo/endfield_yolo_v2.meta.json`）。ultralytics 为 AGPL-3.0，与本插件同协议，无冲突。
- **第三方模型 `EF_items_*`（物品检测，193 类）**：来自 [icaruszezen/Endfield_Vision_Models](https://github.com/icaruszezen/Endfield_Vision_Models)，**AGPL-3.0**，与本插件同协议。类别表与本仓库 `data/yolo/endfield_items_classes.txt` **完全一致（193 类）**。当前仅用于**本地离线**生成 `ui_icon` 伪标签（Round 98 v3c 的 teacher）；同协议下随插件分发在协议上允许，但目前**未内置权重**，由用户按需下载。
- **OCR 本体词表** `data/yolo/wiki_ontology.json`：`_meta.sources` 记录的来源为 warfarin.wiki、gamekee.com/zmd、data.akedata.wiki、wiki.biligame.com/zmd、endfield.huijiwiki.com、github/icaruszezen-Endfield_Vision_Models、game-vfs/TableCfg。**能否再分发与署名方式：TODO（待用户评估，W4）。**
- **ZMDLogs**：MIT 许可，见 `third_party/ZMDLogs-LICENSE.txt`。插件不包含其源码，只作为子进程拉起并订阅本地事件流。
- **GameScaling（M6）**：推理客户端 vendor 自 [THUSI-Lab/GameScaling](https://github.com/THUSI-Lab/GameScaling)（Apache-2.0，见 `third_party/GameScaling-LICENSE.txt`；适配说明在各文件头）。模型权重**不随插件分发**，由用户自行下载并用 vLLM 部署；上游声明为纯科研项目、模型与代码 Apache-2.0、不含任何游戏内容权利。
- MaaEnd / MaaFramework 不随插件分发，由用户自行安装。

## 已知临时方案：#3483

上游 [Project-N-E-K-O/N.E.K.O#3483](https://github.com/Project-N-E-K-O/N.E.K.O/issues/3483)：在入口或定时器里 await `self.plugins.call_entry*` 会静默超时。`maaend/p2p_http.py` 先走宿主回环地址的 HTTP `/runs`（显式绕过系统代理），失败再退回 SDK 调用；`maaend/suite.py` 里用到它的地方都标了 `shortcut: #3483`。**上游修复后删掉 `p2p_http.py`，改回 `self.plugins.call_entry*`。**

## 开发与测试

需要 N.E.K.O 插件 SDK（`plugin` 包）在 `PYTHONPATH` 上，插件目录名必须是 `maa_end`（测试按 `maa_end.maaend` 导入）。需要 pytest 和 pytest-asyncio。

```bash
# 单元测试（conftest 把 #3483 回退指向必拒连的回环端口，测试不会碰真实宿主）
PYTHONPATH=<N.E.K.O 插件 SDK 目录> python -m pytest -q tests

# 桩 SDK 端到端验证；报告写到 tests/stub-report.txt（含本机路径，已在 .gitignore 和打包排除里）
# 设 MAA_FW_BIN=<MaaFramework 运行库目录> 可额外验证真实 DLL，不设则跳过
python tests/maaend_stub_run.py

# 与市场 CI 相同的 lint
uvx ruff==0.12.4 check --ignore-noqa --config ruff.toml .
```

打包规则在 `pyproject.toml` 的 `[tool.neko.build]`：排除本机配置 `config/`、开发文档和 `data/scene/*.pt` 训练检查点。

## 发布

市场仓库名 `n.e.k.o_plugin_maa_end`，tag 与 `plugin.toml` 的版本一致。在插件目录执行：

```bash
neko-plugin check -r .   # 发布门禁（含 tests/）
neko-plugin publish .
```

## English summary

N.E.K.O plugin for *Arknights: Endfield* (**Windows only**): companion/stuck assist, 33-class scene classification, YOLO detection, OCR, MaaEnd task catalog and execution, daily planning, an optional ZMDLogs battle-event stream, and an optional GameScaling VLM agent (M6: frame in → 200 ms keyboard/mouse out, user-served vLLM checkpoint). Read-only by default: task execution is behind `allow_run` (off by default), ZMDLogs packet capture (needs Npcap + admin) is behind `zmdlogs_enabled` (off by default), and the GameScaling agent is behind a triple gate (`gamescaling_enabled` + `allow_run` + confirm). YOLO / OCR need an external Python with `ultralytics` / `rapidocr_onnxruntime`. Task features need a local MaaEnd install. License and data attribution: **AGPL-3.0** (see `LICENSE`); vendored GameScaling client is Apache-2.0 (see `third_party/GameScaling-LICENSE.txt`); `EF_items_*` item models from icaruszezen/Endfield_Vision_Models are AGPL-3.0 (not bundled). Ontology redistribution (W4) still TODO.
