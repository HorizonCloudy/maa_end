"""测试公共设置。"""
import os

# 测试绝不连真宿主：#3483 回退助手默认走 http://127.0.0.1:48916，宿主在跑时会写入真实数据。
# 用必拒连的回环端口兜底；需要假宿主的测试自行 setenv 覆盖。
os.environ.setdefault("NEKO_PLUGIN_HOST_URL", "http://127.0.0.1:0")

# 关于临时目录（2026-11-04 实测，别试图"修"）：
# 沙箱下系统临时目录 `%TEMP%` 是**不可写**的（PermissionError），IDE 注入的
# sitecustomize 因此把 `tempfile.tempdir` 指向 cwd —— 这是沙箱适配，不是 bug。
# 曾经在这里改回系统 temp，结果 pytest basetemp 建不出来，31 个测试直接红。
# 所以：残留的 `report-*/`、`pytest-of-*/` 是可接受的，由 .gitignore 覆盖，
# 真正的减量手段是 tests/maaend_stub_run.py 的 ignore 列表（不拷 .venv 与旧拷贝）。
