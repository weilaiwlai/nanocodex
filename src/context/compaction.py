from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

import tiktoken
from agents import Agent, Runner, SQLiteSession, TResponseInputItem
from pydantic import BaseModel, Field

from src.runtime.paths import display_path, get_default_workspace_root

DEFAULT_CONTEXT_COMPACT_TRIGGER_TOKENS = 12_000
DEFAULT_CONTEXT_COMPACT_MIN_MESSAGES = 8
DEFAULT_CONTEXT_COMPACT_KEEP_RECENT_ITEMS = 12
DEFAULT_CONTEXT_COMPACT_ARCHIVE_DIR = "artifacts/compaction"
DEFAULT_MICRO_COMPACT_MIN_TOOL_RESULTS = 6
DEFAULT_MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS = 3
DEFAULT_MICRO_COMPACT_LONG_RESULT_MIN_CHARS = 600


@dataclass(frozen=True, slots=True)
class HistorySummary:
    # summary 属于 L3，会随着会话推进更新；它不是稳定提示词，也不是仓库规则。
    layer: str
    current_goal: str
    key_constraints_and_decisions: list[str]
    important_files_and_evidence: list[str]
    unfinished_items: list[str]
    active_topics: list[str] = field(default_factory=list)
    errors_encountered: list[str] = field(default_factory=list)
    decisions_rationale: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "current_goal": self.current_goal,
            "key_constraints_and_decisions": list(self.key_constraints_and_decisions),
            "important_files_and_evidence": list(self.important_files_and_evidence),
            "unfinished_items": list(self.unfinished_items),
            "active_topics": list(self.active_topics),
            "errors_encountered": list(self.errors_encountered),
            "decisions_rationale": list(self.decisions_rationale),
        }

    def to_message_text(self) -> str:
        # 写回 session 时只保留一个稳定模板，便于后续继续叠代 summary。
        sections = [
            "## Archived Session Summary",
            "",
            "### Current Goal",
            self.current_goal or "Unknown",
            "",
            "### Key Constraints & Decisions",
            *(
                [f"- {item}" for item in self.key_constraints_and_decisions]
                or ["- None"]
            ),
            "",
            "### Important Files & Evidence",
            *(
                [f"- {item}" for item in self.important_files_and_evidence]
                or ["- None"]
            ),
            "",
            "### Unfinished Items",
            *(
                [f"- {item}" for item in self.unfinished_items]
                or ["- None"]
            ),
        ]
        if self.active_topics:
            sections.extend(["", "### Active Topics", *[f"- {item}" for item in self.active_topics]])
        if self.errors_encountered:
            sections.extend(["", "### Errors Encountered", *[f"- {item}" for item in self.errors_encountered]])
        if self.decisions_rationale:
            sections.extend(["", "### Decisions Rationale", *[f"- {item}" for item in self.decisions_rationale]])
        return "\n".join(sections).strip()


class _HistorySummaryOutput(BaseModel):
    # 结构化输出字段：核心 4 字段 + 扩展 3 字段。
    # 注：部分第三方兼容接口（如 DeepSeek）暂不支持 output_type 结构化输出，
    # generate_history_summary 中已改用普通文本输出并手动解析 JSON，此处保留原模型供参考。
    current_goal: str = Field(default="")
    key_constraints_and_decisions: list[str] = Field(default_factory=list)
    important_files_and_evidence: list[str] = Field(default_factory=list)
    unfinished_items: list[str] = Field(default_factory=list)
    active_topics: list[str] = Field(default_factory=list)
    errors_encountered: list[str] = Field(default_factory=list)
    decisions_rationale: list[str] = Field(default_factory=list)


# DeepSeek 等兼容接口不支持 output_type 结构化输出，这里提供一份可解析的 JSON 模板。
_HISTORY_SUMMARY_JSON_TEMPLATE = """{
    "current_goal": "简短描述当前用户目标",
    "key_constraints_and_decisions": ["约束或决策 1", "约束或决策 2"],
    "important_files_and_evidence": ["文件或证据 1", "文件或证据 2"],
    "unfinished_items": ["未完成的项 1", "未完成的项 2"],
    "active_topics": ["活跃主题 1"],
    "errors_encountered": ["遇到的错误 1"],
    "decisions_rationale": ["决策理由 1"]
}"""


def _parse_summary_json(raw_output: str) -> _HistorySummaryOutput:
    """从普通文本输出中提取 JSON，兼容 markdown 代码块包裹。"""
    # 优先取 markdown 代码块中的 JSON
    text = raw_output.strip()
    if text.startswith("```"):
        # 去除开头的 ```json 或 ```
        lines = text.splitlines()
        if lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # 尝试直接解析 JSON
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # 兜底：尝试取第一个 { 与最后一个 } 之间的内容
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"summary output is not valid JSON: {raw_output[:200]}") from exc
        data = json.loads(text[start : end + 1])

    return _HistorySummaryOutput(
        current_goal=data.get("current_goal", ""),
        key_constraints_and_decisions=data.get("key_constraints_and_decisions", []),
        important_files_and_evidence=data.get("important_files_and_evidence", []),
        unfinished_items=data.get("unfinished_items", []),
        active_topics=data.get("active_topics", []),
        errors_encountered=data.get("errors_encountered", []),
        decisions_rationale=data.get("decisions_rationale", []),
    )


SummaryGenerator = Callable[[list[TResponseInputItem], str], Awaitable[HistorySummary]]


@dataclass(frozen=True, slots=True)
class MicroCompactConfig:
    min_tool_results_before_compact: int
    keep_recent_tool_results: int
    long_result_min_chars: int


@dataclass(frozen=True, slots=True)
class ContextCompactionConfig:
    trigger_tokens: int
    min_messages: int
    keep_recent_items: int
    archive_dir: Path
    micro: MicroCompactConfig


@dataclass(frozen=True, slots=True)
class MicroCompactStats:
    total_tool_results: int
    replaced_tool_results: int


@dataclass(frozen=True, slots=True)
class SessionCompactionResult:
    compacted: bool
    summary: HistorySummary | None
    archive_path: str | None


@dataclass(frozen=True, slots=True)
class PreparedHistory:
    history_items: list[TResponseInputItem]
    summary: HistorySummary | None
    compaction: dict[str, Any]


def _read_positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        parsed = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是正整数。") from exc
    if parsed <= 0:
        raise ValueError(f"{name} 必须是正整数。")
    return parsed


def get_context_compaction_config() -> ContextCompactionConfig:
    # 配置按调用时动态读取，方便开发时直接通过环境变量压低阈值做验证。
    archive_dir = os.environ.get("CONTEXT_COMPACT_ARCHIVE_DIR", DEFAULT_CONTEXT_COMPACT_ARCHIVE_DIR)
    return ContextCompactionConfig(
        trigger_tokens=_read_positive_int_env(
            "CONTEXT_COMPACT_TRIGGER_TOKENS",
            DEFAULT_CONTEXT_COMPACT_TRIGGER_TOKENS,
        ),
        min_messages=_read_positive_int_env(
            "CONTEXT_COMPACT_MIN_MESSAGES",
            DEFAULT_CONTEXT_COMPACT_MIN_MESSAGES,
        ),
        keep_recent_items=_read_positive_int_env(
            "CONTEXT_COMPACT_KEEP_RECENT_ITEMS",
            DEFAULT_CONTEXT_COMPACT_KEEP_RECENT_ITEMS,
        ),
        archive_dir=((get_default_workspace_root() / archive_dir).resolve()),
        micro=MicroCompactConfig(
            min_tool_results_before_compact=_read_positive_int_env(
                "CONTEXT_MICRO_COMPACT_MIN_TOOL_RESULTS",
                DEFAULT_MICRO_COMPACT_MIN_TOOL_RESULTS,
            ),
            keep_recent_tool_results=_read_positive_int_env(
                "CONTEXT_MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS",
                DEFAULT_MICRO_COMPACT_KEEP_RECENT_TOOL_RESULTS,
            ),
            long_result_min_chars=_read_positive_int_env(
                "CONTEXT_MICRO_COMPACT_LONG_RESULT_MIN_CHARS",
                DEFAULT_MICRO_COMPACT_LONG_RESULT_MIN_CHARS,
            ),
        ),
    )


def _item_to_dict(item: TResponseInputItem) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="python")
    return dict(item)


def _is_summary_message(item: TResponseInputItem) -> bool:
    raw_item = _item_to_dict(item)
    return (
        raw_item.get("role") == "system"
        and isinstance(raw_item.get("content"), str)
        and "## Archived Session Summary" in raw_item["content"]
    )


_TOOL_OUTPUT_TYPES = frozenset({
    "function_call_output",
    "local_shell_call_output",
    "shell_call_output",
})


def _get_tool_output_text(item: TResponseInputItem) -> str | None:
    raw_item = _item_to_dict(item)
    item_type = str(raw_item.get("type", ""))
    if item_type not in _TOOL_OUTPUT_TYPES:
        return None
    output = raw_item.get("output")
    return output if isinstance(output, str) else None


def _replace_tool_output(item: TResponseInputItem, output: str) -> TResponseInputItem:
    raw_item = _item_to_dict(item)
    raw_item["output"] = output
    return raw_item


def _build_tool_call_name_map(items: list[TResponseInputItem]) -> dict[str, str]:
    # micro_compact 需要把 call_id 还原成工具名，才能保留"之前用过什么工具"这层线索。
    name_map: dict[str, str] = {}
    for item in items:
        raw_item = _item_to_dict(item)
        if raw_item.get("type") != "function_call":
            continue
        call_id = raw_item.get("call_id")
        name = raw_item.get("name")
        if isinstance(call_id, str) and isinstance(name, str):
            name_map[call_id] = name
    return name_map


def _remove_orphaned_tool_pairs(items: list[TResponseInputItem]) -> list[TResponseInputItem]:
    # 历史被切片或截断后，function_call 和 function_call_output 的 call_id 配对可能断裂。
    # OpenAI API 严格要求每个 function_call_output 都有对应的 function_call，否则返回 400。
    call_ids: set[str] = set()
    for item in items:
        raw_item = _item_to_dict(item)
        if raw_item.get("type") == "function_call":
            call_id = raw_item.get("call_id")
            if isinstance(call_id, str):
                call_ids.add(call_id)

    cleaned: list[TResponseInputItem] = []
    for item in items:
        raw_item = _item_to_dict(item)
        if str(raw_item.get("type", "")) in _TOOL_OUTPUT_TYPES:
            call_id = raw_item.get("call_id")
            if isinstance(call_id, str) and call_id not in call_ids:
                continue
        cleaned.append(item)
    return cleaned


def _build_tool_placeholder(tool_name: str) -> str:
    return f"[Previous tool result: used {tool_name}]"


def micro_compact_history_items(
    items: list[TResponseInputItem],
    *,
    config: MicroCompactConfig | None = None,
) -> tuple[list[TResponseInputItem], MicroCompactStats]:
    # 这一步只生成“本轮送给模型的 L3 视图”，不回写原始 session。
    active_config = config or get_context_compaction_config().micro
    tool_name_map = _build_tool_call_name_map(items)
    tool_result_indices = [
        index
        for index, item in enumerate(items)
        if _get_tool_output_text(item) is not None
    ]  #找出所有 tool_result 项
    if len(tool_result_indices) < active_config.min_tool_results_before_compact:
        return list(items), MicroCompactStats(
            total_tool_results=len(tool_result_indices),
            replaced_tool_results=0,
        )

    keep_from = max(0, len(tool_result_indices) - active_config.keep_recent_tool_results)
    kept_indices = set(tool_result_indices[keep_from:])  #保留最近的 tool_result 项
    compacted_items: list[TResponseInputItem] = []
    replaced_count = 0

    for index, item in enumerate(items):
        output_text = _get_tool_output_text(item)
        if output_text is None or index in kept_indices:
            compacted_items.append(_item_to_dict(item))
            continue

        # 这里只压缩“更早且足够长”的 tool_result，短结果直接保留，避免无意义地损失信息。
        if len(output_text) < active_config.long_result_min_chars:
            compacted_items.append(_item_to_dict(item))
            continue

        raw_item = _item_to_dict(item)
        tool_name = tool_name_map.get(str(raw_item.get("call_id", "")), "tool")
        compacted_items.append(_replace_tool_output(item, _build_tool_placeholder(tool_name)))
        replaced_count += 1

    return compacted_items, MicroCompactStats(
        total_tool_results=len(tool_result_indices),
        replaced_tool_results=replaced_count,
    )


def _serialize_for_tokens(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def estimate_context_tokens(
    *,
    model: str,
    stable_text: str,
    repo_rule_text: str,
    history_items: list[TResponseInputItem],
    current_turn_items: list[TResponseInputItem],
) -> int:
    """用 tiktoken 对 整个上下文 做 token 估算"""
    # 这里先做“调用前本地估算”，避免把是否压缩完全交给上一次 API usage。
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")

    payload = "\n\n".join(
        [
            stable_text,
            repo_rule_text,
            _serialize_for_tokens([_item_to_dict(item) for item in history_items]),
            _serialize_for_tokens([_item_to_dict(item) for item in current_turn_items]),
        ]
    )
    return len(encoding.encode(payload))


def _render_history_for_summary(history_items: list[TResponseInputItem]) -> str:
    rendered_lines: list[str] = []
    for item in history_items:
        raw_item = _item_to_dict(item)
        item_type = str(raw_item.get("type", "message"))
        if item_type == "message":
            role = str(raw_item.get("role", "unknown"))
            rendered_lines.append(f"[{role}] {raw_item.get('content', '')}")
            continue
        rendered_lines.append(_serialize_for_tokens(raw_item))
    return "\n".join(rendered_lines).strip()


async def generate_history_summary(
    history_items: list[TResponseInputItem],
    model: str,
) -> HistorySummary:
    # summary 生成和主 agent 分开跑，避免把压缩提示词污染正常 coding 提示词。
    # 原实现使用 output_type 结构化输出，部分第三方兼容接口（如 DeepSeek）不支持该
    # response_format 类型，会报错 "This response_format type is unavailable now"。
    # 因此改为普通文本输出，让模型返回 JSON，再手动解析。
    #
    # 原代码（已注释）：
    # summary_agent = Agent(
    #     name="history-summary",
    #     model=model,
    #     output_type=_HistorySummaryOutput,
    #     instructions=(
    #         "You summarize long coding-agent conversations.\n"
    #         "Return only structured fields.\n"
    #         "Keep the current user goal, key constraints or decisions, important files or evidence, "
    #         "and unfinished items.\n"
    #         "Prefer short bullet-like phrases."
    #     ),
    # )
    # result = await Runner.run(
    #     summary_agent,
    #     input=(
    #         "Summarize the following session history.\n\n"
    #         f"{_render_history_for_summary(history_items)}"
    #     ),
    # )
    # summary_output = result.final_output
    # if not isinstance(summary_output, _HistorySummaryOutput):
    #     raise TypeError("summary generator returned an unexpected output type")

    summary_agent = Agent(
        name="history-summary",
        model=model,
        instructions=(
            "You summarize long coding-agent conversations.\n"
            "Return ONLY a valid JSON object matching this structure, with no extra text:\n"
            f"{_HISTORY_SUMMARY_JSON_TEMPLATE}\n"
            "Keep the current user goal, key constraints or decisions, important files or evidence, "
            "and unfinished items.\n"
            "Prefer short bullet-like phrases."
        ),
    )
    result = await Runner.run(
        summary_agent,
        input=(
            "Summarize the following session history.\n\n"
            f"{_render_history_for_summary(history_items)}"
        ),
    )
    summary_output = _parse_summary_json(str(result.final_output))

    return HistorySummary(
        layer="L3",
        current_goal=summary_output.current_goal.strip(),
        key_constraints_and_decisions=[
            item.strip()
            for item in summary_output.key_constraints_and_decisions
            if item.strip()
        ],
        important_files_and_evidence=[
            item.strip()
            for item in summary_output.important_files_and_evidence
            if item.strip()
        ],
        unfinished_items=[
            item.strip()
            for item in summary_output.unfinished_items
            if item.strip()
        ],
        active_topics=[
            item.strip()
            for item in summary_output.active_topics
            if item.strip()
        ],
        errors_encountered=[
            item.strip()
            for item in summary_output.errors_encountered
            if item.strip()
        ],
        decisions_rationale=[
            item.strip()
            for item in summary_output.decisions_rationale
            if item.strip()
        ],
    )


def build_summary_message_item(summary: HistorySummary) -> TResponseInputItem:
    return {
        "role": "system",
        "content": summary.to_message_text(),
    }


def _archive_history_items(
    *,
    session_id: str,
    history_items: list[TResponseInputItem],
    archive_dir: Path,
) -> str:
    # 自动压缩前先把完整对话落盘，后面要追查被压缩掉的上下文时有原始证据。
    archive_dir.mkdir(parents=True, exist_ok=True)
    filename = (
        f"session_{session_id}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid4().hex[:8]}.json"
    )
    archive_path = archive_dir / filename
    archive_path.write_text(
        json.dumps([_item_to_dict(item) for item in history_items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return display_path(archive_path, get_default_workspace_root())


async def compact_session_history(
    *,
    session: SQLiteSession,
    session_id: str,
    model: str,
    config: ContextCompactionConfig | None = None,
    summary_generator: SummaryGenerator | None = None,
    force: bool = False,
) -> SessionCompactionResult:
    active_config = config or get_context_compaction_config()
    raw_items = list(await session.get_items())
    if not raw_items:
        return SessionCompactionResult(compacted=False, summary=None, archive_path=None)
    if not force and len(raw_items) < active_config.min_messages:
        return SessionCompactionResult(compacted=False, summary=None, archive_path=None)

    generator = summary_generator or generate_history_summary
    archive_path = _archive_history_items(
        session_id=session_id,
        history_items=raw_items,
        archive_dir=active_config.archive_dir,
    )
    summary = await generator(raw_items, model)

    # session 中只保留一个 summary 项；旧 summary 也会被这次新摘要吸收进来。
    non_summary_items = [item for item in raw_items if not _is_summary_message(item)]
    recent_items = _remove_orphaned_tool_pairs(non_summary_items[-active_config.keep_recent_items :])
    await session.clear_session()
    await session.add_items([build_summary_message_item(summary), *recent_items])
    return SessionCompactionResult(
        compacted=True,
        summary=summary,
        archive_path=archive_path,
    )


async def prepare_history_for_model(
    *,
    session: SQLiteSession,
    session_id: str,
    model: str,
    stable_text: str,
    repo_rule_text: str,
    current_turn_items: list[TResponseInputItem],
    existing_summary: HistorySummary | None = None,
    summary_generator: SummaryGenerator | None = None,
    config: ContextCompactionConfig | None = None,
) -> PreparedHistory:
    # 这是 L3 的统一入口：先做 view 级 micro_compact，再按 token 估算决定是否真的改写 session。
    active_config = config or get_context_compaction_config()  #获取配置
    raw_items = list(await session.get_items()) #读取原始历史
    history_items, micro_stats = micro_compact_history_items(raw_items, config=active_config.micro)
    estimated_tokens = estimate_context_tokens(
        model=model,
        stable_text=stable_text,
        repo_rule_text=repo_rule_text,
        history_items=history_items,
        current_turn_items=current_turn_items,
    ) 
    auto_compacted = False
    archive_path: str | None = None
    summary = existing_summary

    if raw_items and len(raw_items) >= active_config.min_messages and estimated_tokens >= active_config.trigger_tokens:
        compacted = await compact_session_history(
            session=session,
            session_id=session_id,
            model=model,
            config=active_config,
            summary_generator=summary_generator,
        )
        if compacted.compacted:
            auto_compacted = True
            archive_path = compacted.archive_path
            summary = compacted.summary
            raw_items = list(await session.get_items())
            history_items, micro_stats = micro_compact_history_items(raw_items, config=active_config.micro)
            estimated_tokens = estimate_context_tokens(
                model=model,
                stable_text=stable_text,
                repo_rule_text=repo_rule_text,
                history_items=history_items,
                current_turn_items=current_turn_items,
            )

    return PreparedHistory(
        history_items=_remove_orphaned_tool_pairs(history_items),
        summary=summary,
        compaction={
            "token_estimator": "tiktoken",
            "estimated_tokens": estimated_tokens,
            "micro_compacted": micro_stats.replaced_tool_results > 0,
            "tool_result_count": micro_stats.total_tool_results,
            "replaced_tool_results": micro_stats.replaced_tool_results,
            "auto_compacted": auto_compacted,
            "archive_path": archive_path,
        },
    )
