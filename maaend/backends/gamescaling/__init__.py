"""GameScaling 推理客户端（vendor 自 THUSI-Lab/GameScaling，Apache-2.0）。

来源仓：https://github.com/THUSI-Lab/GameScaling
commit：6764efabfbb4121d6e5d3cfcbc54dedf1469bf5a（2026-10 快照）
许可：Apache-2.0，全文见 third_party/GameScaling-LICENSE.txt
模型权重不在本插件内：用户自行从 HuggingFace 下载 checkpoint 并用 vLLM 部署，
插件只做客户端。宿主进程零三方依赖，故 policy.py 已把 requests/PIL 换成
标准库实现（见该文件头「适配」一节）；动作格式与提示词语义逐字保留。
"""

from .action import Action, parse_action
from .keymaps import TO_WIN32_VK
from .policy import Decision, GameScalingPolicy, PolicyConfig, probe_encoder
from .prompt import build_system_prompt

__all__ = [
    "Action",
    "Decision",
    "GameScalingPolicy",
    "PolicyConfig",
    "TO_WIN32_VK",
    "build_system_prompt",
    "parse_action",
    "probe_encoder",
]
