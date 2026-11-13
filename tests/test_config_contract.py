"""配置契约 —— 三份 `[maa]` 配置必须一致，且每个键都得有人读。

背景（docs/REFACTOR-PLAN.md §2 #11/#12）：
- `plugin.toml`（发布默认值）48 键、`config.example.toml`（示例模板）45 键、
  `config/plugin.toml`（本机运行时）33 键 —— 装完可能缺键；
- `fail_demotion_threshold` 三份都声明、代码里 0 处读取（阶段 2 已接进 FailTracker）。

`config/plugin.toml` 是宿主生成的本机运行时配置（`.gitignore` 忽略），
**不参与本测试** —— 它按发布默认值合并，不应手工维护。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "maaend"

#: 允许「声明了但代码不直接读」的键（宿主/打包器消费，或纯文档用途）。
#: 加键请写清原因，不要为了让测试变绿而塞进来。
ALLOWED_UNREAD: frozenset[str] = frozenset()


def _maa_section(name: str) -> dict:
    data = tomllib.loads((ROOT / name).read_text(encoding="utf-8"))
    section = data.get("maa")
    assert isinstance(section, dict), f"{name} 缺 [maa] 段"
    return section


def _source_text() -> str:
    parts = [
        p.read_text(encoding="utf-8", errors="ignore")
        for p in SOURCE_DIR.rglob("*.py")
        if "__pycache__" not in str(p)
    ]
    return "\n".join(parts)


def test_templates_have_same_keys() -> None:
    published = set(_maa_section("plugin.toml"))
    example = set(_maa_section("config.example.toml"))
    assert published == example, (
        f"两份模板键集合不一致 —— 只在 plugin.toml: {sorted(published - example)}；"
        f"只在 config.example.toml: {sorted(example - published)}"
    )


def test_every_key_is_read_somewhere() -> None:
    keys = set(_maa_section("plugin.toml"))
    text = _source_text()
    unread = sorted(k for k in keys if k not in text and k not in ALLOWED_UNREAD)
    assert not unread, f"这些配置键没人读（死配置或拼写错误）：{unread}"


def test_key_count_is_stable() -> None:
    """键数变化必须是有意的：新增/删除配置请同步改这个数字。"""
    assert len(_maa_section("plugin.toml")) == 54
