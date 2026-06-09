from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

from agents import RunContextWrapper, function_tool

from src.protocol import ToolResponse, success_response
from src.runtime.paths import get_default_workspace_root
from src.runtime.session import ToolRuntimeContext
from src.tools.common import (
    ToolFailure,
    build_context,
    build_stats,
    error_from_failure,
    run_traced_tool,
    start_timer,
)
from src.tools.skill_loader import (
    SKILL_NAME_PATTERN,
    SkillLoader,
    _parse_frontmatter,
    get_default_skill_loader,
)


def load_skill_content(
    *,
    name: str,
    args: str = "",
    loader: SkillLoader | None = None,
) -> ToolResponse:
    """按名称加载 skill，并把展开后的正文返回给主代理。"""
    start_time = start_timer()
    params_input = {"name": name, "args": args}
    active_loader = loader or get_default_skill_loader()

    try:
        normalized_name = name.strip()
        if not normalized_name:
            raise ToolFailure(
                code="INVALID_PARAM",
                message="name 参数不能为空。",
                text="参数错误：必须提供 skill 名称。",
            )

        skill = active_loader.render_skill(normalized_name, args)
        if skill is None:
            raise ToolFailure(
                code="NOT_FOUND",
                message=f"未找到 skill '{normalized_name}'。",
                text=f"未找到 skill '{normalized_name}'。",
            )

        # base_dir 前缀是 skill 能引用本目录约定和附属资源的最小上下文锚点。
        content = f"Base directory for this skill: {skill.base_dir}\n\n{skill.body}".strip()

        return success_response(
            data={
                "name": skill.name,
                "description": skill.description,
                "path": skill.path,
                "base_dir": skill.base_dir,
                "content": content,
            },
            text=f"已加载 skill {skill.name}。",
            stats=build_stats(start_time),
            context=build_context(
                params_input=params_input,
                path_resolved=skill.path,
            ),
        )
    except ToolFailure as failure:
        return error_from_failure(
            failure,
            start_time=start_time,
            params_input=params_input,
        )


def _build_skill_md(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


def create_skill_content(
    *,
    name: str,
    description: str,
    body: str = "",
    workspace_root: Path | None = None,
) -> ToolResponse:
    """在 workspace 的 skills 目录下创建一个新的 SKILL.md 文件。"""
    start_time = start_timer()
    params_input = {"name": name, "description": description, "body": body}

    normalized_name = name.strip()
    normalized_description = description.strip()

    if not normalized_name:
        raise ToolFailure(
            code="INVALID_PARAM",
            message="name 参数不能为空。",
            text="参数错误：必须提供 skill 名称。",
        )
    if not SKILL_NAME_PATTERN.fullmatch(normalized_name):
        raise ToolFailure(
            code="INVALID_PARAM",
            message=f"skill 名称 '{normalized_name}' 格式非法。",
            text="参数错误：skill 名称只能包含小写字母、数字和连字符。",
        )
    if not normalized_description:
        raise ToolFailure(
            code="INVALID_PARAM",
            message="description 参数不能为空。",
            text="参数错误：必须提供 skill 描述。",
        )

    active_root = (workspace_root or get_default_workspace_root()).resolve()
    skill_dir = active_root / "skills" / normalized_name
    skill_path = skill_dir / "SKILL.md"

    if skill_path.exists():
        raise ToolFailure(
            code="ALREADY_EXISTS",
            message=f"skill '{normalized_name}' 已存在。",
            text=f"skill '{normalized_name}' 已存在，路径：{skill_path.relative_to(active_root).as_posix()}。如需更新，请使用 Write 工具编辑该文件。",
        )

    skill_dir.mkdir(parents=True, exist_ok=True)
    content = _build_skill_md(normalized_name, normalized_description, body)
    skill_path.write_text(content, encoding="utf-8")

    return success_response(
        data={
            "name": normalized_name,
            "description": normalized_description,
            "path": skill_path.relative_to(active_root).as_posix(),
        },
        text=f"已创建 skill {normalized_name}。",
        stats=build_stats(start_time),
        context=build_context(
            params_input=params_input,
            path_resolved=skill_path.relative_to(active_root).as_posix(),
        ),
    )


def download_skill_content(
    *,
    url: str,
    name: str = "",
    workspace_root: Path | None = None,
) -> ToolResponse:
    """从 URL 下载 skill 内容并保存到 workspace 的 skills 目录下。"""
    start_time = start_timer()
    params_input = {"url": url, "name": name}

    normalized_url = url.strip()
    if not normalized_url:
        raise ToolFailure(
            code="INVALID_PARAM",
            message="url 参数不能为空。",
            text="参数错误：必须提供下载 URL。",
        )
    if not normalized_url.startswith(("http://", "https://")):
        raise ToolFailure(
            code="INVALID_PARAM",
            message="仅支持 http:// 或 https:// 协议的 URL。",
            text="参数错误：URL 协议必须是 http 或 https。",
        )

    # 下载内容
    try:
        with urllib.request.urlopen(normalized_url, timeout=30) as response:
            raw_bytes = response.read()
    except urllib.error.URLError as exc:
        raise ToolFailure(
            code="DOWNLOAD_FAILED",
            message=f"下载失败：{exc}",
            text=f"无法从 '{normalized_url}' 下载 skill 内容，请检查 URL 和网络连接。",
        )

    # 解码
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolFailure(
            code="DECODE_ERROR",
            message="下载内容不是有效的 UTF-8 文本。",
            text="下载的内容无法按 UTF-8 解码，skill 文件必须是纯文本 Markdown。",
        )

    # 验证 frontmatter
    parsed = _parse_frontmatter(text)
    if parsed is None:
        raise ToolFailure(
            code="INVALID_FORMAT",
            message="下载内容缺少合法的 YAML frontmatter。",
            text="下载的 skill 文件格式非法。必须以 '---\\n' 开头，包含 name 和 description 字段，并以 '\\n---\\n' 分隔头部和正文。",
        )

    metadata, body = parsed
    frontmatter_name = metadata.get("name", "").strip()
    frontmatter_description = metadata.get("description", "").strip()

    if not frontmatter_description:
        raise ToolFailure(
            code="INVALID_FORMAT",
            message="skill frontmatter 缺少 description 字段。",
            text="下载的 skill 文件格式非法：frontmatter 中必须包含 description 字段。",
        )

    # 确定最终 name
    requested_name = name.strip()
    if requested_name:
        normalized_name = requested_name
        # 若用户显式指定了 name，替换 frontmatter 中的 name 以保持一致
        if normalized_name != frontmatter_name:
            new_metadata = dict(metadata)
            new_metadata["name"] = normalized_name
            header_lines = [f"{k}: {v}" for k, v in new_metadata.items()]
            text = "---\n" + "\n".join(header_lines) + "\n---\n\n" + body
    else:
        normalized_name = frontmatter_name

    if not normalized_name:
        raise ToolFailure(
            code="INVALID_FORMAT",
            message="skill frontmatter 缺少 name 字段，且调用方未提供 name 参数。",
            text="下载的 skill 文件格式非法：frontmatter 中必须包含 name 字段，或在调用时显式指定 name。",
        )
    if not SKILL_NAME_PATTERN.fullmatch(normalized_name):
        raise ToolFailure(
            code="INVALID_FORMAT",
            message=f"skill 名称 '{normalized_name}' 格式非法。",
            text="skill 名称只能包含小写字母、数字和连字符。",
        )

    active_root = (workspace_root or get_default_workspace_root()).resolve()
    skill_dir = active_root / "skills" / normalized_name
    skill_path = skill_dir / "SKILL.md"

    if skill_path.exists():
        raise ToolFailure(
            code="ALREADY_EXISTS",
            message=f"skill '{normalized_name}' 已存在。",
            text=f"skill '{normalized_name}' 已存在，路径：{skill_path.relative_to(active_root).as_posix()}。如需更新，请使用 Write 工具编辑该文件。",
        )

    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(text, encoding="utf-8")

    return success_response(
        data={
            "name": normalized_name,
            "description": frontmatter_description,
            "path": skill_path.relative_to(active_root).as_posix(),
            "url": normalized_url,
        },
        text=f"已下载并安装 skill {normalized_name}。",
        stats=build_stats(start_time),
        context=build_context(
            params_input=params_input,
            path_resolved=skill_path.relative_to(active_root).as_posix(),
        ),
    )


def _skill_tool(
    ctx: RunContextWrapper[ToolRuntimeContext],
    name: str,
    args: str = "",
) -> ToolResponse:
    # Skill 只是一个普通工具：按需把技能正文取回来，不反向控制主架构。
    params_input = {"name": name, "args": args}
    return run_traced_tool(
        ctx.context,
        tool_name="Skill",
        params_input=params_input,
        invoke=lambda: load_skill_content(
            name=name,
            args=args,
            loader=get_default_skill_loader(
                workspace_root=ctx.context.workspace_root,
                execution_root=ctx.context.execution_root,
            ),
        ),
    )


def _create_skill_tool(
    ctx: RunContextWrapper[ToolRuntimeContext],
    name: str,
    description: str,
    body: str = "",
) -> ToolResponse:
    params_input = {"name": name, "description": description, "body": body}
    return run_traced_tool(
        ctx.context,
        tool_name="CreateSkill",
        params_input=params_input,
        invoke=lambda: create_skill_content(
            name=name,
            description=description,
            body=body,
            workspace_root=ctx.context.workspace_root,
        ),
    )


def _download_skill_tool(
    ctx: RunContextWrapper[ToolRuntimeContext],
    url: str,
    name: str = "",
) -> ToolResponse:
    params_input = {"url": url, "name": name}
    return run_traced_tool(
        ctx.context,
        tool_name="DownloadSkill",
        params_input=params_input,
        invoke=lambda: download_skill_content(
            url=url,
            name=name,
            workspace_root=ctx.context.workspace_root,
        ),
    )


skill_tool = function_tool(
    _skill_tool,
    name_override="Skill",
    description_override="按名称加载项目内的一个 skill，并返回展开后的技能说明。",
)

create_skill_tool = function_tool(
    _create_skill_tool,
    name_override="CreateSkill",
    description_override="在当前工作区的 skills 目录下创建一个新的 skill。需要提供 name、description，可选 body。",
)

download_skill_tool = function_tool(
    _download_skill_tool,
    name_override="DownloadSkill",
    description_override="从指定的 http(s) URL 下载 skill 文件并安装到当前工作区的 skills 目录下。",
)

SKILL_TOOLS = [skill_tool, create_skill_tool, download_skill_tool]
