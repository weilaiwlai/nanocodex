from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

# 这是 agent 自己代码仓库的稳定根目录，只用于源码和内置资源。
AGENT_CODE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_APP_HOME_DIRNAME = ".nanocodex"
DEFAULT_MEMORY_INDEX_FILENAME = "MEMORY.md"
_PROJECT_KEY_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def get_default_workspace_root() -> Path:
    """返回 CLI 启动时的当前工作目录作为默认 workspace 根路径。"""
    return Path.cwd().resolve()


def get_app_home_dir() -> Path:
    """返回跨 session 的用户级持久化目录 (~/.nanocodex/)，用于存放项目记忆等全局状态。"""
    return (Path.home() / DEFAULT_APP_HOME_DIRNAME).resolve()


def _read_git_common_dir(workspace_root: Path) -> Path | None:
    """通过 git rev-parse --git-common-dir 读取仓库的 git common 目录，用于识别同一仓库（包括 worktree 场景）。"""
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(workspace_root),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    if not output:
        return None
    return Path(output).resolve()


def get_workspace_project_identity_root(workspace_root: Path | None = None) -> Path:
    """返回 workspace 的项目身份根目录，优先使用 git 仓库根，非 git 项目则回退到 workspace_root。"""
    active_workspace_root = (workspace_root or get_default_workspace_root()).resolve()
    common_dir = _read_git_common_dir(active_workspace_root)
    if common_dir is None:
        return active_workspace_root
    if common_dir.name == ".git":
        return common_dir.parent.resolve()
    return common_dir.resolve()


def _sanitize_project_key_segment(value: str) -> str:
    """将目录名清洗为只含字母、数字、点、下划线和连字符的 slug，用于生成可读的项目 key 片段。"""
    sanitized = _PROJECT_KEY_SANITIZE_RE.sub("-", value.strip()).strip("-").lower()
    return sanitized or "workspace"


def get_workspace_project_key(workspace_root: Path | None = None) -> str:
    """基于项目身份根目录生成稳定唯一的项目 key（可读 slug + SHA256 前 12 位），用于隔离不同项目的持久化数据。"""
    identity_root = get_workspace_project_identity_root(workspace_root=workspace_root)
    slug = _sanitize_project_key_segment(identity_root.name)
    digest = hashlib.sha256(str(identity_root).encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def get_workspace_memory_dir(workspace_root: Path | None = None) -> Path:
    """返回当前 workspace 对应的长期记忆目录 (~/.nanocodex/projects/<key>/memory/)，属于 L2 持久化层。"""
    project_key = get_workspace_project_key(workspace_root=workspace_root)
    return (get_app_home_dir() / "projects" / project_key / "memory").resolve()


def get_workspace_memory_index_path(workspace_root: Path | None = None) -> Path:
    """返回当前 workspace 的长期记忆索引文件路径 (MEMORY.md)。"""
    return get_workspace_memory_dir(workspace_root=workspace_root) / DEFAULT_MEMORY_INDEX_FILENAME


def display_path(path: Path, *bases: Path | None) -> str:
    """将路径转为可读的相对路径表示，优先相对给定的 base 路径，无法相对时回退为绝对路径。"""
    resolved_path = path.resolve()
    for base in bases:
        if base is None:
            continue
        try:
            return str(resolved_path.relative_to(base.resolve()))
        except ValueError:
            continue
    return str(resolved_path)
