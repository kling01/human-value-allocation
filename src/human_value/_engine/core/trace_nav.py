"""
原始 cleaned trace 树的标准访问工具函数。

规范来源：docs/cleaned_trace_protocol.md §5-6。
这里是唯一的真实实现——Socrates 等下游消费方不应再各自手写同名函数的私有副本，
统一从这里 import，行为不一致时只有一处要改。
"""

from __future__ import annotations

import json
from typing import Optional


def safe_loads(s) -> dict:
    """安全解析 JSON 字符串；空串/空白串/非法内容一律返回 {} 而不是抛异常。"""
    if not s or not isinstance(s, str) or not s.strip():
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


def get_tool_steps(node: dict, tool_name: str) -> list:
    """返回树中所有 name==tool_name 的 TOOL 节点（深度递归）。"""
    results = []
    if node.get("type") == "TOOL" and node.get("name") == tool_name:
        results.append(node)
    for s in node.get("steps") or []:
        results.extend(get_tool_steps(s, tool_name))
    return results


def get_all_tool_steps(node: dict) -> list:
    """返回树中所有 TOOL 节点（深度递归）。"""
    results = []
    if node.get("type") == "TOOL":
        results.append(node)
    for s in node.get("steps") or []:
        results.extend(get_all_tool_steps(s))
    return results


def get_final_ai_message(root: dict) -> str:
    """返回 root.output.messages 中最后一条 ai 消息的 content。"""
    for m in reversed((root.get("output") or {}).get("messages") or []):
        if isinstance(m, dict) and m.get("type") == "ai":
            return m.get("content") or ""
    return ""


def get_conversation_messages(root: dict) -> list:
    """返回 root.input.messages（完整对话历史，含 tool_calls）。"""
    return (root.get("input") or {}).get("messages") or []


def tool_was_called(root: dict, tool_name: str, agent_name: Optional[str] = None) -> bool:
    """判断 trace 中是否调用过指定工具，可按 agent_name 过滤。"""
    steps = get_tool_steps(root, tool_name)
    if agent_name:
        steps = [s for s in steps
                 if (s.get("metadata") or {}).get("agent_name") == agent_name]
    return bool(steps)
