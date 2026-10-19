"""Generic feature subset extracted from agent-eval-v2.

Business-specific routing, style, profile and industry heuristics are excluded.
The shared evaluator consumes only generic conversation facts.
"""
import json
import re
from dataclasses import dataclass, field
from typing import List
_UNSET = object()

def get_messages(trace: dict) -> list:
    """
    从 cleaned trace 的 root.input/output.messages 中提取完整消息列表。
    非 LangGraph 状态节点（如 mid_term_compress，root.input 为 list）返回空列表。
    """
    root = trace.get('root')
    if root is None:
        return []
    inp = root.get('input')
    if isinstance(inp, list):
        return []
    inp = inp or {}
    out = root.get('output') or {}
    in_msgs = inp.get('messages') or []
    out_msgs = out.get('messages') or []
    if not isinstance(in_msgs, list) or not isinstance(out_msgs, list):
        return []
    return list(in_msgs) + list(out_msgs)

def _serialize_val(val, max_len: int) -> str:
    """将任意值序列化为可读字符串并截断。"""
    if val is None:
        return '(空)'
    if isinstance(val, str):
        s = val
    else:
        try:
            s = json.dumps(val, ensure_ascii=False)
        except Exception:
            s = str(val)
    return s[:max_len] + ('...' if len(s) > max_len else '')

def _collect_tool_records(node: dict, result: list) -> None:
    """递归收集 TOOL 节点的 (name, input, output)，按执行顺序。"""
    if (node.get('type') or '').upper() == 'TOOL':
        name = node.get('name') or ''
        if name:
            result.append((name, node.get('input'), node.get('output')))
    for child in node.get('steps') or []:
        _collect_tool_records(child, result)

def _extract_dispatch_details(top_steps: list) -> list:
    """
    从 root.steps 按顺序提取每次 dispatch_subagent 的 subagent 执行细节。
    返回 [(可读字符串块, dispatch节点对象), ...]，每项对应一次 dispatch。
    带上节点对象是为了：块被真正插入输出时，调用方能精确标记该 dispatch 子树的
    TOOL 节点 id 为"已渲染"（marked at insertion time，不是 build time）——
    这样 messages 缺失对应 tool_call、块没被插入的 dispatch，其工具会正确落到 orphan 兜底。
    """
    details = []
    for step in top_steps:
        if (step.get('type') or '').upper() != 'TOOL':
            continue
        if step.get('name') != 'dispatch_subagent':
            continue
        inp = step.get('input') or {}
        if isinstance(inp, str):
            try:
                import ast
                inp = ast.literal_eval(inp)
            except Exception:
                inp = {}
        agent_name = inp.get('agent_name', '?') if isinstance(inp, dict) else '?'
        tool_records: list = []
        for child in step.get('steps') or []:
            _collect_tool_records(child, tool_records)
        lines = [f'\n  ┌── {agent_name} 内部执行 ──────────────']
        if tool_records:
            for t_name, t_inp, t_out in tool_records:
                lines.append(f'  │ [工具] {t_name}')
                lines.append(f'  │   输入: {_serialize_val(t_inp, 4000)}')
                lines.append(f'  │   返回: {_serialize_val(t_out, 6000)}')
        else:
            lines.append('  │ (无内部工具调用记录)')
        subagent_out = step.get('output')
        if subagent_out:
            lines.append(f'  │ [subagent输出] {_serialize_val(subagent_out, 6000)}')
        lines.append(f'  └── {agent_name} 执行完毕 ──────────────')
        details.append(('\n'.join(lines), step))
    return details

def _content_to_str(content) -> str:
    """消息 content 归一化为字符串。

    ⚠ 2026-07-29 修：content 可能是多模态 list（[{"type":"text","text":...}, ...]），
    原实现直接 .strip() 会抛 AttributeError，异常被 run_all_evaluators 吞掉 →
    整批评测静默返回 total_results=0（前端只看到 0 条，无任何报错）。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for seg in content:
            if isinstance(seg, str):
                out.append(seg)
            elif isinstance(seg, dict):
                out.append(str(seg.get('text') or seg.get('content') or (json.dumps(seg, ensure_ascii=False) if seg else '')))
        return '\n'.join((x for x in out if x))
    if content is None:
        return ''
    return str(content)

def get_full_trace_str(trace: dict, max_chars: int=200000) -> str:
    """
    将完整 trace 序列化为 LLM 可读字符串，包含 Master 对话 + subagent 内部工具调用详情。

    {{messages}} 占位符统一指向此函数输出，替代纯 Master messages 序列化，
    让 LLM 能看到 subagent 执行步骤和工具返回。
    """
    root = trace.get('root') or {}
    msgs = get_messages(trace)
    rendered_ids: set = set()
    dispatch_details = _extract_dispatch_details(root.get('steps') or [])
    all_tree_tools: list = []
    for _top in root.get('steps') or []:
        _collect_tool_nodes(_top, all_tree_tools)
    parts: list = []
    dispatch_idx = 0
    for m in msgs:
        role = (m.get('type') or m.get('role') or '?').lower()
        content = _content_to_str(m.get('content')).strip()
        tool_calls = m.get('tool_calls') or []
        tool_call_id = m.get('tool_call_id')
        if role == 'system':
            parts.append(f'[系统提示] {content}')
        elif role in ('human', 'user'):
            parts.append(f'\n[用户] {content}')
        elif role in ('ai', 'assistant'):
            if content:
                parts.append(f'\n[Master输出] {content}')
            for tc in tool_calls:
                tc_name = tc.get('name', '')
                tc_args = tc.get('args') or {}
                if tc_name == 'dispatch_subagent':
                    agent = tc_args.get('agent_name', '?')
                    task = tc_args.get('task') or ''
                    parts.append(f'\n[Master→dispatch] agent={agent}  task={task}')
                    if dispatch_idx < len(dispatch_details):
                        block, disp_node = dispatch_details[dispatch_idx]
                        parts.append(block)
                        _mark_subtree_tool_ids(disp_node, rendered_ids)
                        dispatch_idx += 1
                else:
                    parts.append(f'[Master→{tc_name}] {_serialize_val(tc_args, 4000)}')
                    _mark_next_tool_by_name(all_tree_tools, tc_name, rendered_ids)
        elif role == 'tool' or tool_call_id:
            parts.append(f'  → 返回: {content[:4000]}')
        elif content:
            parts.append(f'[{role}] {content[:4000]}')
    orphan_lines = _extract_orphan_tool_details(root, rendered_ids)
    if orphan_lines:
        parts.append('\n── 本轮其它工具执行（非 dispatch 路径）──')
        parts.extend(orphan_lines)
    result = '\n'.join(parts)
    if len(result) > max_chars:
        result = result[:max_chars] + '\n...(内容过长已截断)'
    return result

def get_langfuse_url(trace: dict) -> str:
    """从 cleaned trace 的 htmlPath 字段提取 Langfuse trace URL（路径或完整 URL）。"""
    return trace.get('langfuse_url') or trace.get('htmlPath') or ''
_ENV_FAIL = re.compile('Plugin file not found|plugin.*not found', re.IGNORECASE)
_REAL_FAIL = re.compile('执行失败|ToolException|ValueError|RuntimeError')

def _f(default=_UNSET, default_factory=None, *, category: str, doc: str=''):
    """dataclass field() 包一层，附带 category 供 feature_registry 生成分类 manifest。"""
    meta = {'category': category, 'doc': doc}
    if default_factory is not None:
        return field(default_factory=default_factory, metadata=meta)
    if default is _UNSET:
        return field(metadata=meta)
    return field(default=default, metadata=meta)

@dataclass
class TraceFeatures:
    trace_id: str = _f(category='meta', doc='完整 trace ID')
    timestamp: str = _f(category='meta', doc='trace 开始时间')
    n_dispatch: int = _f(category='dispatch', doc='本轮调用 dispatch 工具几次')
    agent_names: List[str] = _f(category='dispatch', doc='每次调用的 agent_name（缺失时记为 "?"）')
    n_env_fail: int = _f(category='subagent_returns', doc='环境污染失败（plugin 路径缺失等，部署问题）')
    n_real_fail: int = _f(category='subagent_returns', doc='真实业务失败（非环境原因）')
    n_empty_return: int = _f(category='subagent_returns', doc='subagent 返回空内容')
    n_success_return: int = _f(category='subagent_returns', doc='subagent 正常返回')
    subagent_success_rate: float = _f(category='derived', doc='n_success_return / max(n_dispatch, 1)')
    final_content: str = _f(category='final_output', doc='最后一条 ai 消息的原始 content')
    has_final_output: bool = _f(category='final_output', doc='最终输出非空')
    n_tool_calls_total: int = _f(0, category='tool_metrics', doc='所有 tool 响应消息数（含 dispatch 返回）')
    n_tool_calls_success: int = _f(0, category='tool_metrics', doc='成功响应数（status=="成功" 或内容启发）')
    n_nondispatch_tool_calls: int = _f(0, category='tool_metrics', doc='非 dispatch 工具调用次数（含重复）')
    n_tool_calls_unique: int = _f(0, category='tool_metrics', doc='不重复工具名数')
    n_tool_calls_with_full_params: int = _f(0, category='tool_metrics', doc='参数无空值的调用次数')
    available_tools_count: int = _f(0, category='tool_metrics', doc='trace.available_tools 长度（0=未提供）')
    tool_call_success_rate: float = _f(0.0, category='tool_metrics', doc='n_success / max(n_tool_calls_total, 1)')
    tool_repeat_call_rate: float = _f(0.0, category='tool_metrics', doc='(n_nd - unique) / max(n_nd, 1)')
    tool_utilization_rate: float = _f(0.0, category='tool_metrics', doc='unique / max(available_tools_count, 1)')
    tool_param_completeness_rate: float = _f(1.0, category='tool_metrics', doc='complete / max(n_nd, 1)')
    trace_latency: float = _f(0.0, category='latency', doc='trace 总耗时（秒），来自 root.latency')
    is_env_contaminated: bool = _f(False, category='runtime_meta', doc='本轮有环境污染失败')
    skipped: bool = _f(False, category='runtime_meta', doc='')
    skip_reason: str = _f('', category='runtime_meta', doc='')

def _extract_dispatch(ai_msgs: list, dispatch_tool: str) -> dict:
    """提取 dispatch 调用信息。新增调度相关字段在此处扩展。"""
    n_dispatch = 0
    agent_names = []
    for m in ai_msgs:
        for tc in m.get('tool_calls') or []:
            if tc.get('name') == dispatch_tool:
                n_dispatch += 1
                agent_names.append((tc.get('args') or {}).get('agent_name', '?'))
                task = str((tc.get('args') or {}).get('task', ''))
    return {'n_dispatch': n_dispatch, 'agent_names': agent_names}

def _collect_dispatch_steps_from_tree(node: dict, dispatch_tool: str, out: list) -> None:
    """DFS 收集树中所有 dispatch 工具节点（顺序 = 执行顺序）。"""
    if (node.get('type') or '').upper() == 'TOOL' and node.get('name') == dispatch_tool:
        out.append(node)
    for child in node.get('steps') or []:
        _collect_dispatch_steps_from_tree(child, dispatch_tool, out)

def _extract_dispatch_from_tree(root: dict, dispatch_tool: str) -> dict:
    """
    兜底：消息层没有任何 dispatch tool_call、但 steps 树里有 dispatch 节点时
    （部分适配来源的 messages 与树不同步），从树重建调度特征，避免
    n_dispatch=0 / agent_names=[] 的失真值欺骗依赖这些字段的规则
    （config/common/rules.yaml 的 MULTI_DISPATCH / dead_loop 等）。
    同时用与 _extract_returns 相同的正则对 dispatch 输出做返回分类。
    """
    steps: list = []
    _collect_dispatch_steps_from_tree(root, dispatch_tool, steps)
    agent_names = []
    n_env_fail = n_real_fail = n_empty_return = n_success_return = 0
    for s in steps:
        inp = s.get('input') or {}
        if not isinstance(inp, dict):
            inp = {}
        agent_names.append(inp.get('agent_name', '?'))
        out = s.get('output')
        content = out if isinstance(out, str) else '' if out is None else str(out)
        if _ENV_FAIL.search(content):
            n_env_fail += 1
        elif _REAL_FAIL.search(content):
            n_real_fail += 1
        elif content.strip():
            n_success_return += 1
        else:
            n_empty_return += 1
    return {'dispatch': {'n_dispatch': len(steps), 'agent_names': agent_names}, 'returns': {'n_env_fail': n_env_fail, 'n_real_fail': n_real_fail, 'n_empty_return': n_empty_return, 'n_success_return': n_success_return}}

def _extract_returns(tool_msgs: list) -> dict:
    """对每条 tool 消息分类，统计 subagent 返回情况。新增返回分类在此处扩展。"""
    n_env_fail = n_real_fail = n_empty_return = n_success_return = 0
    for m in tool_msgs:
        content = str(m.get('content', ''))
        if _ENV_FAIL.search(content):
            n_env_fail += 1
        elif _REAL_FAIL.search(content):
            n_real_fail += 1
        elif content.strip():
            n_success_return += 1
        else:
            n_empty_return += 1
    return {'n_env_fail': n_env_fail, 'n_real_fail': n_real_fail, 'n_empty_return': n_empty_return, 'n_success_return': n_success_return}

def _extract_output(ai_msgs: list) -> dict:
    """提取最终 ai 输出。新增输出层字段在此处扩展。"""
    final_content = ''
    if ai_msgs:
        final_content = ai_msgs[-1].get('content', '') or ''
    return {'final_content': final_content, 'has_final_output': bool(final_content.strip())}

def _extract_tool_call_metrics(ai_msgs: list, tool_msgs: list, dispatch_tool: str, available_tools_count: int) -> dict:
    """
    计算通用工具调用指标（与具体业务无关的统计性指标）。

    - 成功率  : tool 响应消息中 status=="成功" 的比例；无 status 字段时回落到内容启发
    - 重复率  : 非 dispatch 调用中重复调用同名工具的比例
    - 参数完整性: 非 dispatch 调用中参数无空值的比例
    - 利用率  : 实际使用的不同工具数 / available_tools_count（0 时不计算）
    """
    nd_names: list = []
    nd_args: list = []
    for m in ai_msgs:
        for tc in m.get('tool_calls') or []:
            name = tc.get('name', '')
            if not name or name == dispatch_tool:
                continue
            nd_names.append(name)
            nd_args.append(tc.get('args') or {})
    n_nd = len(nd_names)
    n_uniq = len(set(nd_names))
    repeat = round((n_nd - n_uniq) / max(n_nd, 1), 4) if n_nd > 0 else 0.0
    util = round(n_uniq / available_tools_count, 4) if available_tools_count > 0 else 0.0

    def _is_full(args: dict) -> bool:
        return all((v is not None and v != '' and (v != []) and (v != {}) for v in args.values()))
    n_full = sum((1 for a in nd_args if _is_full(a)))
    completeness = round(n_full / max(n_nd, 1), 4) if n_nd > 0 else 1.0
    n_resp = len(tool_msgs)
    n_success = 0
    for m in tool_msgs:
        status = m.get('status', '')
        content = str(m.get('content', ''))
        if status:
            if status == '成功':
                n_success += 1
        elif content.strip() and (not _ENV_FAIL.search(content)) and (not _REAL_FAIL.search(content)):
            n_success += 1
    success_rate = round(n_success / max(n_resp, 1), 4) if n_resp > 0 else 1.0
    return {'n_tool_calls_total': n_resp, 'n_tool_calls_success': n_success, 'n_nondispatch_tool_calls': n_nd, 'n_tool_calls_unique': n_uniq, 'n_tool_calls_with_full_params': n_full, 'available_tools_count': available_tools_count, 'tool_call_success_rate': success_rate, 'tool_repeat_call_rate': repeat, 'tool_utilization_rate': util, 'tool_param_completeness_rate': completeness}

def _collect_step_tool_names_ordered(node: dict) -> list:
    """DFS 遍历单个 step 节点（含其所有子节点），按访问顺序收集所有 TOOL 节点的工具名。"""
    results = []
    if (node.get('type') or '').upper() == 'TOOL':
        name = node.get('name') or ''
        if name:
            results.append(name)
    for child in node.get('steps') or []:
        results.extend(_collect_step_tool_names_ordered(child))
    return results

def _derive(dispatch: dict, returns: dict) -> dict:
    """从已提取的原始指标计算派生指标。只做数值运算，不访问 trace 原始数据。"""
    n_dispatch = dispatch['n_dispatch']
    n_success = returns['n_success_return']
    return {'subagent_success_rate': round(n_success / max(n_dispatch, 1), 4)}

def extract(trace: dict) -> TraceFeatures:
    base_meta = {"trace_id": str(trace.get("id", "")),
                 "timestamp": str(trace.get("timestamp", ""))[:19]}
    msgs = get_messages(trace)
    if not msgs:
        return TraceFeatures(**base_meta, n_dispatch=0, agent_names=[], n_env_fail=0,
            n_real_fail=0, n_empty_return=0, n_success_return=0, subagent_success_rate=0.0,
            final_content="", has_final_output=False, skipped=True, skip_reason="no_messages")
    dispatch_tool = trace.get("dispatch_tool_name", "dispatch_subagent")
    ai_msgs = [m for m in msgs if m.get("type") == "ai"]
    tool_msgs = [m for m in msgs if m.get("type") == "tool"]
    root = trace.get("root") or {}
    dispatch = _extract_dispatch(ai_msgs, dispatch_tool)
    returns = _extract_returns(tool_msgs)
    if dispatch["n_dispatch"] == 0:
        tree = _extract_dispatch_from_tree(root, dispatch_tool)
        if tree["dispatch"]["n_dispatch"]:
            dispatch = tree["dispatch"]
            returns = tree["returns"]
    metrics = _extract_tool_call_metrics(ai_msgs, tool_msgs, dispatch_tool, len(trace.get("available_tools", [])))
    return TraceFeatures(**base_meta, **dispatch, **returns, **_derive(dispatch, returns),
        **_extract_output(ai_msgs), **metrics, is_env_contaminated=returns["n_env_fail"] > 0,
        trace_latency=float(root.get("latency") or 0.0))


def _mark_subtree_tool_ids(node: dict, ids: set) -> None:
    """把 node 及其整棵子树内所有 TOOL 节点的 id 记入 ids。"""
    if (node.get('type') or '').upper() == 'TOOL':
        ids.add(id(node))
    for child in node.get('steps') or []:
        _mark_subtree_tool_ids(child, ids)

def _mark_next_tool_by_name(all_tools: list, name: str, ids: set) -> None:
    """把按序第一个 name 匹配且尚未渲染的 TOOL 节点标记为已渲染。

    主循环把 Master 层非 dispatch 工具从 messages 渲染成 [Master→X]，但不经树；
    orphan 兜底从树里捞未渲染节点，会把同一次调用再渲染一遍 → 判词误判"同一调用被重复记录"。
    这里按「名字 + 出现顺序」把对应树节点标记掉，保证一次调用只渲染一次。"""
    for t in all_tools:
        if id(t) in ids:
            continue
        if t.get('name') == name:
            ids.add(id(t))
            return

def _collect_tool_nodes(node: dict, out: list) -> None:
    """按执行顺序（DFS 前序）收集树中所有 TOOL 节点对象本身。"""
    if (node.get('type') or '').upper() == 'TOOL':
        out.append(node)
    for child in node.get('steps') or []:
        _collect_tool_nodes(child, out)

def _extract_orphan_tool_details(root: dict, rendered_ids: set) -> list:
    """
    兜底：遍历整棵 steps 树，输出所有【还没被 dispatch 详情实际渲染过】的 TOOL 节点
    （root 直挂 / execute_plan_tasks 子树 / 甚至 messages 缺失对应 tool_call 的 dispatch
    子树）。保证全视角序列化——每个工具节点恰好被渲染一次，一个都不漏。
    """
    all_tools: list = []
    for top in root.get('steps') or []:
        _collect_tool_nodes(top, all_tools)
    lines = []
    for t in all_tools:
        if id(t) in rendered_ids:
            continue
        name = t.get('name') or '?'
        if name == 'dispatch_subagent':
            inp = t.get('input') or {}
            agent = inp.get('agent_name', '?') if isinstance(inp, dict) else '?'
            lines.append(f'  [dispatch→{agent}]')
            lines.append(f'    输入: {_serialize_val(inp, 4000)}')
            lines.append(f"    返回: {_serialize_val(t.get('output'), 6000)}")
        else:
            lines.append(f'  [工具] {name}')
            lines.append(f"    输入: {_serialize_val(t.get('input'), 4000)}")
            lines.append(f"    返回: {_serialize_val(t.get('output'), 6000)}")
    return lines
