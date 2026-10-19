"""
core/meta.py — 数据源指纹工具。

每个 output 目录写入一个 _meta.json，记录本次评测使用的 normalized 数据文件路径和
MD5 hash（取前 12 位）。report.py 在合并 detail.json 与 llm_results.json 时读取
各自目录的 _meta.json 并比对 source_hash，不一致时打印警告。
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Optional


def compute_hash(path: Path) -> str:
    """计算文件内容 MD5，返回前 12 位十六进制字符串。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def write_meta(out_dir: Path, source_path: Path, stage: str, n_traces: int) -> dict:
    """
    在 out_dir 写入 _meta.json，返回写入的 meta dict。

    参数：
        out_dir     — 输出目录（已存在）
        source_path — 评测使用的 normalized JSON 文件路径
        stage       — "rules" | "llm"
        n_traces    — 本次处理的 trace 总数
    """
    meta = {
        "stage":        stage,
        "source_file":  str(source_path),
        "source_hash":  compute_hash(source_path),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_traces":     n_traces,
    }
    (out_dir / "_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return meta


def read_meta(json_path: Path) -> Optional[dict]:
    """
    读取与 json_path 同目录的 _meta.json，不存在则返回 None。

    参数：
        json_path — detail.json 或 llm_results.json 的路径
    """
    meta_path = json_path.parent / "_meta.json"
    if not meta_path.exists():
        return None
    with open(meta_path, encoding="utf-8") as f:
        return json.load(f)
