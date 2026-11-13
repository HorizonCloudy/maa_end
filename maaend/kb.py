"""终末地知识库查询层（只读）。

数据由 `tools/kb/` 的离线采集器写入 `plugin_database` 的 SQLite 库；
本模块只读，不建表、不写入，连接以 `mode=ro` 打开——查询绝不可能污染知识库。

设计取舍：
- 直接用 sqlite3 而不是走 `plugin_database:db_fetch_all` 的跨插件 IPC。
  理由：一次问答要跑 2-3 条带 JOIN 的查询，IPC 每次都要序列化往返；
  而两者读写的是**同一个文件**（SQLite 的 WAL 允许多进程并发读）。
  写入侧仍归 plugin_database（它是唯一 owner）。
- 全部方法同步实现，调用方用 `asyncio.to_thread` 包一层——sqlite3 会阻塞事件循环。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

#: 默认库位置：插件目录 → plugins/ → plugin_database/data/plugin_database.db
_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_DB = _PLUGIN_DIR.parent / "plugin_database" / "data" / "plugin_database.db"

#: 允许查询的表（白名单，避免把参数拼进表名）
_TABLES = ("ef_doc", "ef_entity", "ef_maanode", "ef_recipe", "ef_tool", "ef_alias")


def _db_candidates():
    """按优先级给出可能的库位置。

    宿主可能从 `.neko-plugin-installations/plugins/<id>` 加载代码，而
    `plugin_database` 的 data 目录仍留在源码树（实测：副本里没有 data/，
    源码树的 db 在宿主运行期间持续被写）。所以不能只看同级目录，
    要向上回溯 `plugins/` 与 `plugin_database/` 两种组合。
    """
    yield _PLUGIN_DIR / "data" / "kb" / "plugin_database.db"  # 本地覆盖（可选）
    yield _DEFAULT_DB
    for up in _PLUGIN_DIR.parents:
        yield up / "plugins" / "plugin_database" / "data" / "plugin_database.db"
        yield up / "plugin_database" / "data" / "plugin_database.db"


def resolve_db_path(explicit: str = "") -> Path:
    """库路径解析顺序：显式参数 > 环境变量 MAAEND_KB_DB > 候选路径中第一个存在的。"""
    for cand in (explicit, os.environ.get("MAAEND_KB_DB", "")):
        if cand:
            p = Path(cand).expanduser()
            if p.is_file():
                return p
    fallback = _DEFAULT_DB
    for p in _db_candidates():
        if p.is_file():
            return p
    return fallback


def available(db_path: str = "") -> bool:
    return resolve_db_path(db_path).is_file()


def _connect(db_path: str = "") -> sqlite3.Connection:
    p = resolve_db_path(db_path)
    if not p.is_file():
        raise FileNotFoundError(f"知识库未初始化：{p}（运行 tools/kb/build_kb.py）")
    c = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _rows(cur) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# 查询能力
# ---------------------------------------------------------------------------


def stats(db_path: str = "") -> dict:
    c = _connect(db_path)
    try:
        out: dict[str, Any] = {}
        for t in _TABLES:
            try:
                out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                out[t] = None
        out["by_category"] = _rows(c.execute(
            "SELECT source, category, COUNT(*) AS n FROM ef_doc "
            "GROUP BY source, category ORDER BY n DESC"))
        out["last_ingest"] = _rows(c.execute(
            "SELECT ts, stage, source, detail FROM ef_ingest ORDER BY id DESC LIMIT 5"))
        return out
    finally:
        c.close()


def search(q: str, limit: int = 8, db_path: str = "") -> dict:
    """全文检索。trigram 要求 ≥3 字符，短词自动退化为 LIKE。"""
    q = (q or "").strip()
    if not q:
        return {"query": q, "hits": [], "mode": "empty"}
    c = _connect(db_path)
    try:
        hits: list[dict] = []
        mode = "fts"
        if len(q) >= 3:
            try:
                hits = _rows(c.execute(
                    "SELECT d.id, d.source, d.category, d.title, d.url, "
                    "snippet(ef_doc_fts, 1, '', '', '…', 20) AS snippet "
                    "FROM ef_doc_fts f JOIN ef_doc d ON d.id = f.rowid "
                    "WHERE ef_doc_fts MATCH ? ORDER BY rank LIMIT ?", (q, max(1, limit))))
            except sqlite3.OperationalError:
                hits = []
        if not hits:
            mode = "like"
            hits = _rows(c.execute(
                "SELECT id, source, category, title, url, substr(body_md, 1, 200) AS snippet "
                "FROM ef_doc WHERE title LIKE ? OR body_md LIKE ? LIMIT ?",
                (f"%{q}%", f"%{q}%", max(1, limit))))
        for h in hits:
            h["snippet"] = " ".join((h.get("snippet") or "").split())[:240]
        return {"query": q, "mode": mode, "hits": hits}
    finally:
        c.close()


def entity(name: str, db_path: str = "") -> dict:
    """按中文名 / 英文名 / 游戏 ID / slug 查实体（先精确，再模糊）。"""
    name = (name or "").strip()
    if not name:
        return {"query": name, "entities": []}
    c = _connect(db_path)
    try:
        rows = _rows(c.execute(
            "SELECT kind, slug, name, name_en, rarity, game_id, meta_json FROM ef_entity "
            "WHERE name = ? OR name_en = ? OR game_id = ? OR slug = ?",
            (name, name, name, name)))
        exact = True
        if not rows:
            exact = False
            rows = _rows(c.execute(
                "SELECT kind, slug, name, name_en, rarity, game_id, meta_json FROM ef_entity "
                "WHERE name LIKE ? OR name_en LIKE ? LIMIT 10", (f"%{name}%", f"%{name}%")))
        for r in rows:
            try:
                r["meta"] = json.loads(r.pop("meta_json") or "{}")
            except json.JSONDecodeError:
                r["meta"] = {}
        # 附带别名命中，方便「玩家说外号、系统找正名」
        alias = _rows(c.execute(
            "SELECT alias, kind, target FROM ef_alias WHERE alias LIKE ? LIMIT 5", (f"%{name}%",)))
        return {"query": name, "exact": exact, "entities": rows, "alias_hits": alias}
    finally:
        c.close()


def operators(rarity: str = "", db_path: str = "") -> dict:
    c = _connect(db_path)
    try:
        sql = ("SELECT name, name_en, rarity, game_id, meta_json FROM ef_entity "
               "WHERE kind = 'operator'")
        args: list[Any] = []
        if rarity:
            sql += " AND rarity LIKE ?"
            args.append(f"{rarity.rstrip('★')}%")
        sql += " ORDER BY rarity DESC, name"
        out = []
        for r in c.execute(sql, args):
            m = json.loads(r["meta_json"] or "{}")
            out.append({
                "name": r["name"], "name_en": r["name_en"], "rarity": r["rarity"],
                "game_id": r["game_id"], "class": m.get("class", ""),
                "element": m.get("element", ""), "weapon": m.get("weapon_type", ""),
            })
        return {"count": len(out), "operators": out}
    finally:
        c.close()


def recipe(item: str, limit: int = 30, db_path: str = "") -> dict:
    """配方查询：按产出物品 / 输入物品 / 配方 ID 反查。"""
    item = (item or "").strip()
    if not item:
        return {"query": item, "recipes": []}
    c = _connect(db_path)
    try:
        rows = _rows(c.execute(
            "SELECT id, name_key, machine_id, recipe_type, duration_s, inputs_json, outputs_json "
            "FROM ef_recipe WHERE outputs_json LIKE ? OR inputs_json LIKE ? OR id LIKE ? LIMIT ?",
            (f"%{item}%", f"%{item}%", f"%{item}%", max(1, limit))))
        for r in rows:
            for k in ("inputs_json", "outputs_json"):
                try:
                    r[k.replace("_json", "")] = json.loads(r.pop(k) or "[]")
                except json.JSONDecodeError:
                    r[k.replace("_json", "")] = []
        return {"query": item, "count": len(rows), "recipes": rows}
    finally:
        c.close()


def node(q: str, limit: int = 15, db_path: str = "") -> dict:
    """MaaEnd 界面节点按描述检索（「这个操作在哪个界面」）。"""
    q = (q or "").strip()
    if not q:
        return {"query": q, "nodes": []}
    c = _connect(db_path)
    try:
        rows = _rows(c.execute(
            "SELECT node, desc, action, next_json FROM ef_maanode WHERE desc LIKE ? LIMIT ?",
            (f"%{q}%", max(1, limit))))
        for r in rows:
            try:
                r["next"] = json.loads(r.pop("next_json") or "[]")[:5]
            except json.JSONDecodeError:
                r["next"] = []
        return {"query": q, "count": len(rows), "nodes": rows}
    finally:
        c.close()


def tools(keyword: str = "", status: str = "", db_path: str = "") -> dict:
    """查工具清单（ef_tool）。

    参数：
    - `keyword`：在 name / kind / purpose / notes 里模糊匹配。**必需**——
      24 条工具全列出来对调用方没有意义，且会把真正相关的那条淹掉。
    - `status`：按状态精确过滤（available / in_use / reference / partial /
      api_unavailable / not_integrated）。
    - 都不给：返回全部（调试用）。

    2026-11-05 修：原实现把第一个参数当 status 用，导致 `kb_query(action="tool",
    query="排轴")` 实际执行 `WHERE status='排轴'` —— 永远空结果；`count` 也恒为 0。
    """
    c = _connect(db_path)
    try:
        sql = "SELECT name, url, kind, purpose, access, status, notes FROM ef_tool"
        where: list[str] = []
        args: list[Any] = []
        if status:
            where.append("status = ?")
            args.append(status)
        if keyword:
            where.append("(name LIKE ? OR kind LIKE ? OR purpose LIKE ? OR notes LIKE ?)")
            like = "%" + keyword.strip() + "%"
            args.extend([like, like, like, like])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY CASE status WHEN 'in_use' THEN 0 WHEN 'available' THEN 1 ELSE 2 END, name"
        rows = _rows(c.execute(sql, args))
        return {"count": len(rows), "tools": rows}
    finally:
        c.close()


def _load_table(name: str, db_path: str = "") -> dict:
    """按 `ef_gametable.local` 读一张配置表的整份 JSON（正文不进库，读磁盘）。"""
    c = _connect(db_path)
    try:
        row = c.execute("SELECT local FROM ef_gametable WHERE name = ?", (name,)).fetchone()
    finally:
        c.close()
    if not row or not row[0]:
        return {}
    path = Path(row[0])
    if not path.is_absolute():
        path = _PLUGIN_DIR / path
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


#: 基质词条 ID → 标签键。实测对照：
#:   `gat_passive_attr_str` → `attr_str`
#:   `gst_passive_tactic`   → `tactic`
#: 前缀有两套（gat_ / gst_），都带 `_passive_`，逐个剥。
_TERM_PREFIXES = ("gat_passive_", "gst_passive_", "gat_", "gst_")


def _term_to_tag(term_id: str) -> str:
    for pre in _TERM_PREFIXES:
        if term_id.startswith(pre):
            return term_id[len(pre):]
    return term_id


def gem_weapons(gem_id: str = "", tags: str = "", limit: int = 20, db_path: str = "") -> dict:
    """基质 → 适配武器（基质反查）。

    数据链（全部来自本地解包表，不联网）：

        GemPresetTable          基质预设：gemItemId → termList（3 个词条 + 等级）
          ↓ 剥前缀
        标签组合                attr_str + attr_atk + tactic
          ↓
        GemTagKeyToWeaponTable  68 条：标签组合 → 武器 ID 列表
          ↓
        WeaponBasicTable        84 把武器 → 名称/稀有度

    两条入口：
    - `gem_id`：基质 ID（如 `gem_claym_0003_442`）
    - `tags`：直接给标签组合，`+` 或空格分隔（`attr_str+attr_atk+tactic`）

    2026-11-05 实测：`GemPresetTable` 184 条、`GemTagKeyToWeaponTable` 68 条。
    注意 `ForesightWeaponGemwishlistTable` 只有 1 条（"唤月"），是前瞻功能的数据，
    **不能**当作完整映射用 —— 别被表名骗了。
    """
    presets = _load_table("GemPresetTable", db_path)
    mapping = _load_table("GemTagKeyToWeaponTable", db_path)
    weapons = _load_table("WeaponBasicTable", db_path)

    def weapon_name(wid: str) -> str:
        """武器显示名。

        `WeaponBasicTable` 里**没有 `name` 字段**，只有 `engName`（形如
        `{"id": ..., "text": "Industry 0.1"}`）。中文名要去 `I18nTextTable_CN`
        按 id 查，但那张表 13.1 MB / 16 万条，为一次查询加载不值得 ——
        实测抽查中 `engName.text` 与 i18n 结果一致（部分武器本就未本地化）。
        需要严格中文名时，可另写一个带缓存的 i18n 查询。
        """
        w = weapons.get(wid) or {}
        for field in ("name", "engName"):
            nm = w.get(field)
            if isinstance(nm, dict) and nm.get("text"):
                return str(nm["text"])
            if isinstance(nm, str) and nm:
                return nm
        return wid

    def pack(key: str, ids: list[str]) -> dict:
        return {"tags": key,
                "weapons": [{"id": i, "name": weapon_name(i)} for i in ids[:limit]],
                "count": len(ids)}

    # 入口 1：直接给标签
    if tags:
        key = "+".join(t.strip() for t in str(tags).replace(" ", "+").split("+") if t.strip())
        hit = mapping.get(key)
        if hit is None:                     # 容忍顺序不同
            want = set(key.split("+"))
            for k, v in mapping.items():
                if set(k.split("+")) == want:
                    key, hit = k, v
                    break
        if hit is None:
            return {"found": False, "tags": key, "hint": "标签组合不存在；可用 gametable('GemTagKeyToWeaponTable') 看全部 68 组"}
        return {"found": True, **pack(key, list(hit.get("list") or []))}

    # 入口 2：给基质 ID
    if gem_id:
        preset = presets.get(gem_id)
        if preset is None:                  # 容忍模糊
            cand = [k for k in presets if gem_id in k][:1]
            preset = presets.get(cand[0]) if cand else None
            gem_id = cand[0] if cand else gem_id
        if preset is None:
            return {"found": False, "gem": gem_id, "hint": "基质预设不存在；可先 gametable('GemPresetTable') 查 ID"}
        term_ids = [_term_to_tag(str(t.get("termId") or "")) for t in (preset.get("termList") or [])]
        term_ids = [t for t in term_ids if t]
        key = "+".join(term_ids)
        ids = list((mapping.get(key) or {}).get("list") or [])
        if not ids:                         # 顺序不一定一致，按集合匹配
            want = set(term_ids)
            for k, v in mapping.items():
                if set(k.split("+")) == want:
                    key, ids = k, list(v.get("list") or [])
                    break
        return {"found": bool(ids), "gem": gem_id, "terms": term_ids,
                **pack(key, ids), "rarity": preset.get("rarity")}

    return {"found": False, "hint": "需要 gem_id 或 tags；例：gem_weapons(tags='attr_str+attr_atk+tactic')"}


def poi(kind: str = "", area: str = "", near: str = "", limit: int = 10,
        db_path: str = "") -> dict:
    """查地图兴趣点（LevelMapMark 解出：2,579 个点 / 54 种类型，带世界坐标）。

    参数：
    - `kind`：类型，中文或英文 templateId 模糊匹配（「宝箱」/「mark_detector」）
    - `area`：区域 levelId 模糊匹配（如 `map01_lv002`）
    - `near`：`"x,y,z"` 或 `"x y z"`，给了就按欧氏距离由近到远排
    - 都不给：返回类型分布统计

    坐标是游戏内世界坐标，与 MaaEnd 的 MapNavigator 同一套。
    """
    c = _connect(db_path)
    try:
        if not kind and not area and not near:
            rows = _rows(c.execute(
                "SELECT kind_cn, kind, COUNT(*) AS n, COUNT(DISTINCT area) AS areas "
                "FROM ef_poi GROUP BY kind_cn, kind ORDER BY n DESC"))
            total = c.execute("SELECT COUNT(*) FROM ef_poi").fetchone()[0]
            return {"total_poi": total, "kinds": rows}

        sql = ("SELECT inst_id, area, kind, kind_cn, x, y, z, tier, is_mist "
               "FROM ef_poi WHERE 1=1")
        args: list[Any] = []
        if kind:
            sql += " AND (kind_cn LIKE ? OR kind LIKE ?)"
            args += [f"%{kind}%", f"%{kind}%"]
        if area:
            sql += " AND area LIKE ?"
            args.append(f"%{area}%")

        coords = None
        if near:
            parts = [p for p in near.replace(",", " ").split() if p]
            if len(parts) >= 2:
                try:
                    coords = tuple(float(p) for p in parts[:3])
                except ValueError:
                    coords = None

        if coords:
            # 距离在 SQL 里算：SQLite 3.35+ 有 sqrt，老的没有就用平方和排序
            px, py, pz = (list(coords) + [0, 0])[:3]
            sql += " ORDER BY ((x-?)*(x-?)) + ((y-?)*(y-?)) + ((z-?)*(z-?)) ASC LIMIT ?"
            args += [px, px, py, py, pz, pz, max(1, min(100, limit))]
        else:
            sql += " ORDER BY area, kind_cn LIMIT ?"
            args.append(max(1, min(100, limit)))

        rows = _rows(c.execute(sql, args))
        if coords and rows:
            for r in rows:
                dx = r["x"] - coords[0]
                dy = r["y"] - (coords[1] if len(coords) > 1 else 0)
                dz = r["z"] - (coords[2] if len(coords) > 2 else 0)
                r["dist"] = round((dx * dx + dy * dy + dz * dz) ** 0.5, 1)
        return {"count": len(rows), "near": near or None, "poi": rows}
    finally:
        c.close()


def doc(category: str = "", slug: str = "", db_path: str = "") -> dict:
    """取单篇文档正文（按分类 + slug）。"""
    if not slug:
        return {"doc": None}
    c = _connect(db_path)
    try:
        sql = ("SELECT source, category, slug, title, url, game_version, fetched_at, "
               "body_md, truncated FROM ef_doc WHERE slug = ?")
        args: list[Any] = [slug]
        if category:
            sql += " AND category = ?"
            args.append(category)
        row = c.execute(sql, args).fetchone()
        return {"doc": dict(row) if row else None}
    finally:
        c.close()


def gamefiles(query: str = "", grp: str = "", limit: int = 20, db_path: str = "") -> dict:
    """查解包产物索引（只返回元信息；正文在磁盘，按 `local` 读）。

    数据来自本机客户端的 VFS 容器（本地研究副本，不随插件分发）。
    """
    c = _connect(db_path)
    try:
        sql = "SELECT grp, path, ext, size, local FROM ef_gamefile WHERE 1=1"
        args: list[Any] = []
        if grp:
            sql += " AND grp = ?"
            args.append(grp)
        if query:
            sql += " AND path LIKE ?"
            args.append(f"%{query}%")
        sql += " ORDER BY size DESC LIMIT ?"
        args.append(max(1, min(200, limit)))
        rows = _rows(c.execute(sql, args))
        total = c.execute("SELECT COUNT(*) FROM ef_gamefile").fetchone()[0]
        by_grp = _rows(c.execute(
            "SELECT grp, COUNT(*) AS n, SUM(size) AS bytes FROM ef_gamefile GROUP BY grp"))
        return {"count": len(rows), "total_indexed": total, "by_group": by_grp, "files": rows}
    finally:
        c.close()


def gametable(name: str = "", key: str = "", limit: int = 5, db_path: str = "") -> dict:
    """查游戏配置表（SparkBuffer → JSON，已填中文）。

    - 不给 `name`：返回表清单（按条目数倒序），用于「游戏里有哪些表」
    - 给 `name` 不给 `key`：返回该表的元信息 + 前 `limit` 条
    - 给 `name` + `key`：返回该 key 的完整条目

    正文不进库，按 `ef_gametable.local` 读磁盘（`table_cn/` 有 329 MB）。
    """
    c = _connect(db_path)
    try:
        if not name:
            rows = _rows(c.execute(
                "SELECT name, kind, entries, size FROM ef_gametable "
                "ORDER BY entries DESC LIMIT ?", (max(1, min(200, limit * 8)),)))
            return {"tables": rows,
                    "total": c.execute("SELECT COUNT(*) FROM ef_gametable").fetchone()[0]}

        # 先精确匹配，再模糊
        row = c.execute(
            "SELECT name, kind, entries, fields, size, local FROM ef_gametable WHERE name = ?",
            (name,)).fetchone()
        if row is None:
            row = c.execute(
                "SELECT name, kind, entries, fields, size, local FROM ef_gametable "
                "WHERE name LIKE ? ORDER BY entries DESC LIMIT 1", (f"%{name}%",)).fetchone()
        if row is None:
            return {"error": f"未找到表 {name!r}", "hint": "用 name='' 列出全部表"}
        meta = {
            "name": row["name"], "kind": row["kind"], "entries": row["entries"],
            "fields": json.loads(row["fields"] or "[]"), "size": row["size"],
            "local": row["local"],
        }
    finally:
        c.close()

    path = (Path(meta["local"]) if Path(meta["local"]).is_absolute()
            else _PLUGIN_DIR / meta["local"])
    if not path.is_file():
        return {"meta": meta, "error": f"本地文件缺失：{path}"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"meta": meta, "error": f"读取失败：{exc}"}

    if key:
        val = data.get(key) if isinstance(data, dict) else None
        if val is None and isinstance(data, list):
            for it in data:
                if isinstance(it, dict) and key in (
                        str(it.get("id", "")), str(it.get("name", ""))):
                    val = it
                    break
        return {"meta": meta, "key": key, "found": val is not None,
                "value": (json.dumps(val, ensure_ascii=False)[:6000] if val is not None else None)}

    if isinstance(data, dict):
        keys = list(data.keys())[: max(1, limit)]
        sample = {k: data[k] for k in keys}
    else:
        sample = data[: max(1, limit)]
    return {"meta": meta, "sample_keys": list(sample.keys())[: limit] if isinstance(sample, dict) else None,
            "sample": json.dumps(sample, ensure_ascii=False)[:6000]}
