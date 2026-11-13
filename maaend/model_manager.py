"""maa_end 模型管理 —— SHA-256 校验 / 按显卡选依赖 / 引擎缓存目录。

设计约束(与项目其余部分一致):
- **纯标准库可 import**:hashlib/pathlib/json/os/shutil。torch 只在
  detect_device() 里懒加载,模块导入零开销(插件启动不能被拖慢);
- **不自动联网**:maa_end 的模型随插件附带(data/yolo/),所以默认只做
  "解析 + 校验 + 缓存",不静默下载。确实要下载时显式调 download_model()
  并给出期望的 sha256 —— 下载后先验再落盘,验不过就删掉,不留坏文件;
- **只读优先**:本模块不写模型目录,只写 .cache/(引擎缓存)。

参考来源: JayziLi/endfield 的模型管理做法(下载后核对 SHA-256 再放入
models/、按 NVIDIA/CPU 分别装依赖、TensorRT 引擎与测速结果进 .cache/
且不被 git 跟踪)。本模块只取其正当工程部分,不涉及该仓库的输入注入。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

# 引擎/预处理缓存目录名(与上游同款约定:缓存不入库、不随插件分发)。
CACHE_DIRNAME = ".cache"

# 已知的附带模型期望校验和。留空表示"未登记,只算不比对"。
# 新增模型时用 `python -m maaend.model_manager --sha <file>` 取实际值填入。
# 2026-11-02 实测登记(用 `python -m maaend.model_manager --sha <file>` 取值)。
# 未登记的模型不会因此被拒 —— 只返回实际 sha256,由调用方决定是否信任。
KNOWN_SHA256: dict[str, str] = {
    "endfield_yolo_v2.pt":
        "78000899f6de746393babb18ecf15c156beecbf0c65de79c4a02faf715605aad",
    "endfield_yolo_v2_960.pt":
        "db2458bbda5dfcbe182c3b2923168631e60745257929dffa571ce37a06655116",
}


class ModelError(RuntimeError):
    """模型缺失 / 校验失败 / 下载失败。"""


# ---------------------------------------------------------------- 路径


def models_dir(plugin_dir: Path | str | None = None) -> Path:
    """模型目录。默认 <插件根>/data/yolo/。"""
    if plugin_dir is not None:
        return Path(plugin_dir) / "data" / "yolo"
    here = Path(__file__).resolve().parent          # maaend/
    return here.parent / "data" / "yolo"            # <plugin>/data/yolo


def cache_dir(plugin_dir: Path | str | None = None) -> Path:
    """引擎缓存目录(<模型目录>/.cache/),不存在则创建。"""
    d = models_dir(plugin_dir) / CACHE_DIRNAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass                                        # 只读环境:返回路径即可
    return d


# ---------------------------------------------------------------- 校验


def sha256_file(path: Path | str, chunk_size: int = 1 << 20) -> str:
    """流式算 SHA-256(大文件不整体读进内存)。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def verify_model(
    path: Path | str,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """校验模型文件。

    expected 为空时只返回实际值(不算失败),用于"先登记再比对"的场景。
    返回 {ok, exists, path, size, sha256, expected, reason}。
    """
    p = Path(path)
    out: dict[str, Any] = {
        "ok": False, "exists": p.is_file(), "path": str(p),
        "size": None, "sha256": None, "expected": expected_sha256, "reason": None,
    }
    if not p.is_file():
        out["reason"] = "文件不存在"
        return out
    try:
        out["size"] = p.stat().st_size
        out["sha256"] = sha256_file(p)
    except OSError as exc:
        out["reason"] = "读取失败: " + str(exc)[:120]
        return out
    if not expected_sha256:
        out["ok"] = True
        out["reason"] = "未登记期望值,仅返回实际 sha256"
        return out
    if out["sha256"].lower() != expected_sha256.strip().lower():
        out["reason"] = "SHA-256 不匹配"
        return out
    out["ok"] = True
    return out


# ---------------------------------------------------------------- 设备


def detect_device() -> str:
    """选推理设备:'cuda' 或 'cpu'。

    顺序: torch.cuda.is_available()(准) → nvidia-smi 探测(无 torch 时)
    → cpu(都没有)。任何异常都安全退回 cpu,绝不因此崩插件。
    """
    try:
        import torch  # 懒加载;插件进程里可能根本没有 torch

        if torch.cuda.is_available():
            return "cuda"
        return "cpu"
    except Exception:
        pass
    if shutil.which("nvidia-smi"):
        try:
            import subprocess

            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return "cuda"
        except Exception:
            pass
    return "cpu"


# ---------------------------------------------------------------- 解析


def ensure_model(
    name_or_path: str,
    plugin_dir: Path | str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """解析模型并校验。

    - 传绝对路径/已存在文件 → 直接用;
    - 传文件名 → 在 models_dir 下找;
    - expected 省略时查 KNOWN_SHA256 登记表。

    返回 verify_model() 的结果,并附 device 与 cache_dir。
    """
    mdir = models_dir(plugin_dir)
    p = Path(name_or_path)
    if not p.is_file():
        cand = mdir / name_or_path
        if cand.is_file():
            p = cand
    exp = expected_sha256 or KNOWN_SHA256.get(Path(name_or_path).name)
    res = verify_model(p, exp)
    res["models_dir"] = str(mdir)
    res["cache_dir"] = str(cache_dir(plugin_dir))
    res["device"] = detect_device()
    return res


# ---------------------------------------------------------------- 下载(显式调用)


def download_model(
    url: str,
    dest: Path | str,
    expected_sha256: str,
    timeout: float = 120.0,
    max_bytes: int = 1 << 30,     # 1 GiB 上限,防异常大文件
) -> dict[str, Any]:
    """下载模型 → 先验 SHA-256 → 通过才落盘。验不过就删掉,不留坏文件。

    本函数**不会被自动调用**:maa_end 的模型随插件附带,是否需要联网
    下载由调用方(用户/入口)显式决定。
    """
    from urllib.request import urlopen

    dest = Path(dest)
    tmp = dest.with_suffix(dest.suffix + ".part")
    out: dict[str, Any] = {"ok": False, "path": str(dest), "reason": None}
    h = hashlib.sha256()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        with urlopen(url, timeout=timeout) as r, open(tmp, "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                total += len(b)
                if total > max_bytes:
                    raise ModelError("超过大小上限: " + str(max_bytes))
                h.update(b)
                f.write(b)
        actual = h.hexdigest()
        out["size"] = total
        out["sha256"] = actual
        if actual.lower() != expected_sha256.strip().lower():
            raise ModelError("SHA-256 不匹配: 期望 " + expected_sha256 + " 实际 " + actual)
        os.replace(tmp, dest)      # 原子替换,避免半截文件被当成完整模型
        out["ok"] = True
    except Exception as exc:
        out["reason"] = str(exc)[:200]
        try:
            if tmp.exists():
                tmp.unlink()       # 清理半截文件
        except OSError:
            pass
    return out


# ---------------------------------------------------------------- 引擎缓存


def engine_cache_key(
    model_sha256: str,
    imgsz: int,
    device: str = "cuda",
    half: bool = True,
) -> str:
    """TensorRT/导出引擎的缓存键。

    决定因素:模型内容(sha256) + 输入尺寸 + 设备 + 精度。任一变了就
    不能复用旧引擎 —— 否则会用错 shape 的引擎,结果全错。
    """
    raw = f"{model_sha256.lower()}|{int(imgsz)}|{device}|{'fp16' if half else 'fp32'}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def engine_path(
    model_sha256: str,
    imgsz: int,
    device: str = "cuda",
    half: bool = True,
    plugin_dir: Path | str | None = None,
) -> Path:
    """引擎缓存文件(.engine),落在 cache_dir 下,文件名即缓存键。"""
    return cache_dir(plugin_dir) / (engine_cache_key(model_sha256, imgsz, device, half) + ".engine")


# ---------------------------------------------------------------- CLI


def _main(argv: list[str]) -> int:
    import json as _json

    if len(argv) >= 3 and argv[1] == "--sha":
        print(sha256_file(argv[2]))
        return 0
    if len(argv) >= 2 and argv[1] == "--check":
        name = argv[2] if len(argv) >= 3 else "endfield_yolo_v2.pt"
        print(_json.dumps(ensure_model(name), ensure_ascii=False, indent=2))
        return 0
    print("用法:\n  python -m maaend.model_manager --sha <file>\n"
          "  python -m maaend.model_manager --check [模型名]")
    return 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
