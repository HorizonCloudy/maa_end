"""插件级常量 —— router 与各 mixin 共用。

为什么单独一个文件：拆 mixin 后，`api/*.py` 需要 `DOWNLOAD_URL`、`CONFIG_PREFIX`
这类常量，但**不能** `from ..router import ...`（会造成 router ↔ mixin 循环导入）。
常量放这里，两边都往下依赖。
"""

from __future__ import annotations

#: 配置段名。宿主 config.dump() 返回 {"maa": {...}}。
CONFIG_PREFIX = "maa"

#: 官网下载入口：未安装时给用户一条可执行的路。
DOWNLOAD_URL = "https://maaend.com/"
