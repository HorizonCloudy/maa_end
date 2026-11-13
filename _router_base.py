"""Router 公共基类：PluginRouter 本体不带 data_path/cache_path。

宿主的 PluginRouter 只提供入口收集与 main_plugin 访问；数据目录访问器在主插件上。
本插件四个子系统的 router 都需要 data_path/cache_path，这里统一转发，调用点保持原样。
"""

from __future__ import annotations

from pathlib import Path

from plugin.sdk.plugin import PluginRouter


class _GameRouter(PluginRouter):
    """带主插件数据目录访问器的 router 基类。"""

    def data_path(self, *parts) -> Path:
        return self.main_plugin.data_path(*parts)

    def cache_path(self, *parts) -> Path:
        return self.main_plugin.cache_path(*parts)

    def report_status(self, status):
        return self.main_plugin.report_status(status)
