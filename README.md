# xcode

xcode 是一个面向**本地代码仓库**的智能编程助手，基于 OpenAI Agents SDK 构建。与云端 Code Agent 不同，xcode 深度整合本地环境，具备 workspace 级长期记忆和精细化的上下文治理能力，让 AI 能够真正理解你的代码库，成为你日常开发中的可靠搭档。

## 核心亮点

### workspace 级长期记忆
xcode 为每个项目维护独立的记忆空间。首次分析代码库后，重要信息会被持久化存储，后续对话无需重复解释项目背景，AI 能够"记住"你的代码结构和开发习惯。

### 自适应上下文治理
长会话是代码助手的常见痛点。xcode 通过 L1 / L2 / L3 分层压缩策略，自动管理上下文长度，在保持对话连贯性的同时避免信息膨胀。micro_compact、auto_compact 和手动 compact 三种模式并存，适配不同场景需求。

### 结构化 Tracing 与可追查性
所有运行时事件均有记录，traces 输出为 `jsonl` 和可交互 `html` 两种格式。工具调用的输入输出完整保存，支持事后回溯分析——当 AI 的操作不符合预期时，你可以清晰定位问题所在。

### 统一工具协议
读、写、编辑、搜索、计划、任务管理、Bash 执行等所有工具遵循统一的响应协议。工具输出自动截断、落盘，并在需要时可回查完整内容，确保 AI 在工具使用上的可预测性和可审计性。

### 灵活的 Session 管理
支持创建可命名、可选择、可恢复的会话。你可以为不同任务开启独立 session，也可以在任意时刻中断并后续接续。session 可绑定特定项目目录，也可以共享当前工作目录。

### Skills 扩展机制
通过 `SkillLoader` 和 `Skill` 协议，xcode 支持动态加载自定义技能。目前内置了 code-review 和 repo-explore 两个 skill，你可以根据需要扩展更多场景。

## 快速开始

```bash
# 安装依赖
pip install -e .

# 复制环境配置模板
cp .env.example .env
# 编辑 .env，填入你的 API 配置

# 启动 CLI
python -m scripts.cli

# 或绑定特定项目目录启动
python -m scripts.cli --workspace /path/to/your/project
```

## 技术架构

```
src/
├── runtime/      # Agent 运行时：session 管理、runner、tracing
├── tools/        # 工具实现：读写编辑、搜索、任务管理、Bash 等
├── context/      # 上下文分层、压缩、@file 预处理
├── tasks/        # 任务图、子代理、后台执行、Agent Team
└── protocol/     # 统一工具响应协议
```

## 产物与日志

运行过程中产生的 traces 保存在 `artifacts/traces/` 目录下，包含 `jsonl` 格式的完整事件流和 `html` 格式的可视化回放。Workspace 级记忆存储在 `~/.xx-coding/projects/<project-key>/memory/`。

---

xcode 仍在持续迭代中，欢迎提出问题和改进建议。