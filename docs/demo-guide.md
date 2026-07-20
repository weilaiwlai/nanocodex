# NanoCodex 演示速查卡（Demo Guide）

面向"向同事/他人展示本项目"场景。包含**启动方式**、**重点功能话术**与**预期现象**，
照着念指令即可现场跑通。所有指令默认在 `d:\nanocodex` 目录下执行。

---

## 0. 演示前的准备

1. `.env` 已填 `OPENAI_API_KEY`（非官方接口另填 `OPENAI_BASE_URL`）。
   确认 `TRACE_ENABLED=true`（默认即开，trace 才能生成）。
2. 激活环境（二选一）：
   - 已 `conda init` 并重开终端：`conda activate nanocodex`
   - 否则用绝对路径：`D:\miniconda\envs\nanocodex\python.exe`
3. 终端用 UTF-8 字体，避免中文乱码。TUI 用下方脚本会自动切编码。
4. 演示"被分析对象"最省事就是**分析项目自己**：`--workspace d:\nanocodex`。

---

## 1. 三种启动形态

| 形态 | 适合场景 | 命令 |
|------|----------|------|
| **TUI（推荐）** | 现场 demo，React Ink 界面最有冲击力，含可视化 trace 时间线 | `powershell -ExecutionPolicy Bypass -File scripts/start-tui.ps1` |
| CLI 交互式 | 讲技术细节、看工具调用流式日志 | `python scripts/cli.py --workspace d:\nanocodex` |
| 单条命令 | 录屏/快速验证 | `python scripts/cli.py --workspace d:\nanocodex "你的问题"` |

其他 CLI 参数：
- `--session <名称>`：命名/恢复一个 session
- `--new-session`：强制新建 session
- `--list-sessions`：列出全部 session
- `--json-events`：事件以 JSON 输出（便于二次处理）

---

## 2. 核心亮点（必讲）

### 2.1 仓库规则 / 长期记忆自动注入（第一印象）
让 agent 先"认识"项目：
```
repo-explore 扫描当前仓库并生成项目概览
```
**预期**：自动读懂 `src/` 结构、关键模块。
**讲解点**：`.nanocodex/rules/` 下的多文件规则（回退 `code_law.md`）会自动注入上下文（L2 层），
无需每次重复背景。`src/context/context_builder.py` 负责这套分层。

### 2.2 真实干活（统一工具协议）
```
给 src/tools/read_only.py 的 ls 函数补一段 docstring，并说明改动
```
**预期**：终端流式显示 `[Tool] Edit ...` → `[ToolResult] ...`，
工具输出自动落盘到 `artifacts/tool-output/`，可回查。

### 2.3 跨会话长期记忆（差异化亮点）
退出后开新 session 再问：
```
之前分析的 nanocodex 项目里，上下文压缩模块在哪？
```
**预期**：无需你重复背景即可回答。
**讲解点**：记忆存于 `~/.nanocodex/projects/<project-key>/memory/`（含 `MEMORY.md` 索引与 `topics/` 分类），
是 workspace 级、跨 session 持久化的 L2 记忆。

### 2.4 可追查性（最适合同事看）
跑完任务后打开 trace：
```
# 浏览器打开最近一次 trace
start artifacts/traces\<最新的>.html
```
**预期**：可视化时间线，逐节点看工具输入/输出。
**讲解点**：对应 README「结构化 Tracing」——事件流同时写 `jsonl`（完整）与 `html`（可回放），
支持跨会话对比，AI 行为"出问题可追溯"。

### 2.5 Session 管理
```
python scripts/cli.py --list-sessions
```
**预期**：列出命名 session，展示其可选、可恢复。

---

## 3. 协作 / 工程化功能（进阶展示）

> 以下挂在 root agent 上，用自然语言描述任务，模型自动调用对应工具。
> 工具集：`README_TOOLS / FILE_EDIT / TODO / BASH / COMPACTION / TASK / WORKTREE / SKILL / TEAM`。

### 3.1 多 Agent 协作（Agent Team）— 最强差异点
```
派一个 teammate，名字叫 doc-writer，角色是"文档工程师"，
让它根据 src/tools 下的代码生成一份工具能力清单，写到 docs/tools.md
```
随后：
```
列出当前的 teammate          # 看状态 idle/working
让 doc-writer 把进度汇报给我  # 触发 SendMessage
结束 doc-writer              # 触发 ShutdownRequest
```
**讲解点**：`team-lead` 派生长寿命 teammate，每个有独立 SQLiteSession、独立 worktree、消息队列，
可从 `team_state.json` 恢复未完成 worker（`src/tasks/agent_team.py`）。

### 3.2 任务编排 + 子代理 + 后台执行（Task）
```
创建一个任务：分析 src/context 下的压缩逻辑，然后立刻用后台命令跑一下 pytest
```
涉及工具：`TaskCreate / TaskUpdate / TaskList`（持久化任务图，可设 `blockedBy/blocks`）、
`TaskRun`（轻量模型同步跑分析型子代理）、`BackgroundRun`（后台跑 `pytest`/`git status` 立即返回 task_id，不阻塞）。

### 3.3 Git Worktree 隔离
```
为"重构 compaction 模块"这个任务创建一个 worktree，改完后再合并回主分支
```
**讲解点**：为任务绑定独立分支目录，closeout 支持 `keep/remove/merge` 回主分支——并行改代码互不污染。

### 3.4 上下文压缩（显式触发）
```
现在对话已经很长了，先压缩一下当前会话历史，再继续下面的任务
```
**讲解点**：除自动的 micro_compact（连续 6+ 工具结果 >600 字符）、auto_compact（token 达 12000）外，
模型可显式调用 `Compact` 把旧历史归档、生成 L3 summary 写回（见 README「压缩模式」表）。

### 3.5 Bash 安全沙箱（"为什么可信"）
```
帮我执行 rm -rf / 清理一下磁盘
```
**预期**：模型调用 Bash 并返回"命令被安全规则阻止"。
**讲解点**：Bash 限制工作区边界，主动拦截危险命令：`rm -rf /`、网络命令（`curl/wget/ssh`）、
提权（`sudo/dd`）、交互式命令（`vim/top`）。直观的可信度演示点。

### 3.6 Skill 可插拔扩展
```
加载 code-review 这个 skill，然后用它审查一下 src/tools/bash_tool.py 的安全性
```
涉及工具：`Skill`（加载 `code-review`/`repo-explore`）、`CreateSkill`（agent 自建技能）、
`DownloadSkill`（从 URL 安装到 `skills/`）。

### 3.7 小功能
- **TodoWrite**：多步骤 coding 任务计划跟踪，完成时自动归档（存 `artifacts/todos/`）。
- **@file 提及**：对话里写 `@src/tools/registry.py` 可把该文件内容注入上下文。

---

## 4. 五分钟现场剧本（推荐顺序）

1. 启动 TUI（`scripts/start-tui.ps1`）→ 2.1 仓库规则扫描（第一印象）
2. 2.2 真实改代码（工具协议 + 落盘回查）
3. 3.5 Bash 沙箱拦截（"为什么可信"，最讨喜）
4. 3.1 Agent Team 派一个 teammate（最强差异点）
5. 2.4 打开 html trace 复盘（可追查性）
6. 2.3 新 session 跨会话记忆问答（收尾亮点）

---

## 5. 让他人复现本项目

他人只需：装 uv（或 conda）→ `uv sync`（或 `pip install ...`）→
`copy .env.example .env` 填 key → `python scripts/cli.py`。
`uv.lock` 已锁定版本，复现无障碍。

---

## 附：关键路径速查

| 内容 | 路径 |
|------|------|
| 运行时产物 | `artifacts/`（sessions / traces / tool-output / compaction / todos） |
| 可视化 trace | `artifacts/traces/*.html`（同时有 `* .jsonl`） |
| 仓库规则 | `.nanocodex/rules/`（回退 `code_law.md`） |
| 长期记忆 | `~/.nanocodex/projects/<project-key>/memory/`（含 `MEMORY.md`、`topics/`） |
| 内置技能 | `skills/code-review/`、`skills/repo-explore/` |
| 代码规范 | `code_law.md` |
