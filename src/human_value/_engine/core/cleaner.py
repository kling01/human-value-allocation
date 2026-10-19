"""
cleaner.py

对原始 Langfuse trace JSON 执行五步清洗，输出带树状结构的语义化轨迹。

用法：
    python cleaner.py --input raw.json --output cleaned.json
"""

import json
import argparse
from collections import defaultdict


# ── Step 1：删除 callbacks ────────────────────────────────────────────────────

def step1_delete_callbacks(data: list) -> list:
    def strip(d):
        if isinstance(d, dict) and "callbacks" in d:
            del d["callbacks"]

    for trace in data:
        strip(trace.get("input"))
        strip(trace.get("output"))
        for obs in trace.get("observations", []):
            strip(obs.get("input"))
            strip(obs.get("output"))
    return data


# ── Step 2：删除 trace 根字段 ─────────────────────────────────────────────────

def step2_delete_trace_root(data: list) -> list:
    for trace in data:
        for field in ["input", "output", "metadata"]:
            trace.pop(field, None)
    return data


# ── Step 3：flat obs → 递归树 ─────────────────────────────────────────────────

def _attach_children(obs, children_map):
    node = dict(obs)
    kids = children_map.get(obs["id"], [])
    node["steps"] = [_attach_children(k, children_map) for k in kids]
    return node


def _build_tree(obs_list: list):
    children_map = defaultdict(list)
    for o in obs_list:
        children_map[o.get("parentObservationId")].append(o)
    for pid in children_map:
        children_map[pid].sort(key=lambda x: x.get("startTime") or "")

    roots = [o for o in obs_list if o.get("parentObservationId") is None]
    if len(roots) != 1:
        return None
    return _attach_children(roots[0], children_map)


def step3_build_tree(data: list) -> list:
    result = []
    for trace in data:
        obs_list = trace.get("observations", [])
        tree = _build_tree(obs_list)
        if tree is None:
            continue
        item = {k: v for k, v in trace.items() if k != "observations"}
        item["root"] = tree
        result.append(item)
    return result


# ── Step 4：裁剪基础设施节点 ──────────────────────────────────────────────────

_TRANSPARENT_NAMES = {"invoke_llm", "execute_tools"}
_SKIP_NAMES = {"_route"}


def _prune(node: dict) -> list:
    name = node.get("name", "")

    if name in _SKIP_NAMES:
        return []

    if name in _TRANSPARENT_NAMES:
        result = []
        for child in node.get("steps", []):
            result.extend(_prune(child))
        return result

    pruned = dict(node)
    pruned["steps"] = []
    for child in node.get("steps", []):
        pruned["steps"].extend(_prune(child))
    return [pruned]


def step4_prune_infra(data: list) -> list:
    result = []
    for item in data:
        roots = _prune(item["root"])
        if len(roots) != 1:
            continue
        item["root"] = roots[0]
        result.append(item)
    return result


# ── Step 5：清理冗余字段 ──────────────────────────────────────────────────────

_REDUNDANT_IO_KEYS = {"metadata", "user_context"}


def _is_langgraph(node: dict) -> bool:
    return node.get("type") == "CHAIN" and node.get("name") == "LangGraph"


def _clean_node(node: dict, redundant_meta_keys: set, is_root: bool = False) -> dict:
    node = dict(node)

    if not is_root and node.get("metadata"):
        node["metadata"] = {
            k: v for k, v in node["metadata"].items()
            if k not in redundant_meta_keys
        }

    if _is_langgraph(node):
        in_msgs = []

        if isinstance(node.get("input"), dict):
            inp = dict(node["input"])
            in_msgs = inp.get("messages", [])
            for k in _REDUNDANT_IO_KEYS:
                inp.pop(k, None)
            node["input"] = inp

        if isinstance(node.get("output"), dict):
            out = dict(node["output"])
            out_msgs = out.get("messages", [])
            for k in _REDUNDANT_IO_KEYS:
                out.pop(k, None)
            if out_msgs and in_msgs:
                out["messages"] = out_msgs[len(in_msgs):]
            node["output"] = out

    node["steps"] = [
        _clean_node(child, redundant_meta_keys) for child in node.get("steps", [])
    ]
    return node


def step5_clean_fields(data: list) -> list:
    for item in data:
        root = item["root"]
        redundant_meta_keys = set((root.get("metadata") or {}).keys())
        item["root"] = _clean_node(root, redundant_meta_keys, is_root=True)
    return data


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_pipeline(data: list) -> list:
    data = step1_delete_callbacks(data)
    data = step2_delete_trace_root(data)
    data = step3_build_tree(data)
    data = step4_prune_infra(data)
    data = step5_clean_fields(data)
    return data


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="对原始 Langfuse trace JSON 执行五步清洗")
    parser.add_argument("--input",  required=True, help="原始 trace JSON 文件（loader.py 的输出）")
    parser.add_argument("--output", required=True, help="输出 JSON 文件路径")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    print(f"读取：{args.input}，共 {len(data)} 条 trace")

    data = run_pipeline(data)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    print(f"完成！共 {len(data)} 条 → {args.output}")


if __name__ == "__main__":
    main()
