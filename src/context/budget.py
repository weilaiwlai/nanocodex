"""Per-model-call context limits, including tool schemas and output reserve."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from uuid import uuid4


class ContextBudgetExceeded(RuntimeError):
    pass


def count_tokens(value, model='') -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    try:
        import tiktoken
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding('cl100k_base')
        return len(encoding.encode(text, disallowed_special=()))
    except ImportError:
        # Conservative fallback for offline tests; not advertised as exact usage.
        return (len(text.encode('utf-8')) + 2) // 3


def complete_tool_pairs(items):
    call_types = {'function_call', 'custom_tool_call'}
    output_types = {'function_call_output', 'custom_tool_call_output'}
    calls = {i.get('call_id') for i in items if i.get('type') in call_types}
    outputs = {i.get('call_id') for i in items if i.get('type') in output_types}
    return [i for i in items if i.get('type') not in call_types | output_types or i.get('call_id') in calls & outputs]


def input_budget() -> int:
    window = int(os.getenv('NANOCODEX_CONTEXT_WINDOW_TOKENS', '32768'))
    reserve = int(os.getenv('NANOCODEX_OUTPUT_RESERVE_TOKENS', '4096'))
    configured = int(os.getenv('NANOCODEX_INPUT_BUDGET_TOKENS', '24000'))
    if reserve < 1 or configured < 512 or window <= reserve:
        raise ValueError('上下文预算配置无效。')
    return min(configured, window - reserve)


async def govern_input(items, *, instructions='', schemas=(), model='', budget=None,
                       archive_dir: Path | None = None, summary_generator=None, emit=None):
    limit = input_budget() if budget is None else budget
    overhead = count_tokens({'instructions': instructions, 'tools': schemas}, model) + 256
    cost = lambda data: overhead + count_tokens(data, model)
    original = copy.deepcopy(items)
    data = copy.deepcopy(items)
    if cost(data) <= limit:
        return data
    archive = None
    if archive_dir is not None:
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive = archive_dir / f'model-input-{uuid4().hex}.json'
        archive.write_text(json.dumps(original, ensure_ascii=False, default=str), encoding='utf-8')
    # Large outputs must not grow the next model request without bound.
    for item in data:
        if item.get('type') in {'function_call_output', 'custom_tool_call_output'}:
            value = item.get('output', '')
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            if len(text) > 1600:
                item['output'] = text[:1000] + f'\n[输出已压缩；原文: {archive or "未持久化"}]\n' + text[-400:]
    if cost(data) > limit:
        last_user = max((i for i, item in enumerate(data) if item.get('role') == 'user'), default=0)
        # Keep the current request and all its call/output pairs intact.
        prefix, recent = complete_tool_pairs(data[:last_user]), complete_tool_pairs(data[last_user:])
        if prefix and summary_generator is not None:
            # Bound each summary request, then summarize summaries if necessary.
            chunk, summaries = [], []
            for item in prefix:
                if count_tokens([*chunk, item], model) > max(512, limit // 2) and chunk:
                    summaries.append((await summary_generator(chunk, model)).to_message_text())
                    chunk = []
                if count_tokens(item, model) > max(512, limit // 2):
                    raise ContextBudgetExceeded('单条历史消息超出摘要预算；请缩小输入或提高预算。')
                chunk.append(item)
            if chunk:
                summaries.append((await summary_generator(chunk, model)).to_message_text())
            summary_text = '\n\n'.join(summaries)
            data = [{'role': 'system', 'content': summary_text}, *recent]
    if cost(data) > limit:
        raise ContextBudgetExceeded(f'上下文需要约 {cost(data)} tokens，输入预算为 {limit}；请缩小输入或提高预算。')
    if emit:
        emit('context_compacted', dict(before_tokens=cost(original), after_tokens=cost(data),
                                      budget_tokens=limit, archive_path=str(archive) if archive else None))
    return data


def build_model_input_filter(context, model):
    async def filter_input(call):
        from agents.run_config import ModelInputData
        from src.context.compaction import generate_history_summary
        schemas = [dict(name=t.name, description=getattr(t, 'description', ''),
                        parameters=getattr(t, 'params_json_schema', {})) for t in call.agent.tools]
        data = await govern_input(call.model_data.input, instructions=call.model_data.instructions or '',
                                  schemas=schemas, model=model,
                                  archive_dir=context.compaction_dir if context is not None else None,
                                  summary_generator=generate_history_summary,
                                  emit=context.emit_runtime_event if context is not None else None)
        return ModelInputData(input=data, instructions=call.model_data.instructions)
    return filter_input
