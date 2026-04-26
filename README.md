# xcode

xcode 是一个面向**本地代码仓库**的智能编程助手，基于 OpenAI Agents SDK 构建。与云端 Code Agent 不同，xcode 深度整合本地环境，具备 workspace 级长期记忆和精细化的上下文治理能力，让 AI 能够真正理解你的代码库，成为你日常开发中的可靠搭档。

## 核心亮点

### workspace 级长期记忆
xcode 为每个项目维护独立的记忆空间。首次分析代码库后，重要信息会被持久化存储，后续对话无需重复解释项目背景，AI 能够"记住"你的代码结构和开发习惯。这意味着：
- 跨会话保持对项目的理解，避免重复上下文构建
- 自动学习项目的代码风格、架构模式和依赖关系
- 针对特定项目的建议更加精准和个性化

### 自适应上下文治理
长会话是代码助手的常见痛点。xcode 通过 L1 / L2 / L3 分层压缩策略，自动管理上下文长度，在保持对话连贯性的同时避免信息膨胀。
- **L1 层**：最近的对话内容，保持详细和完整
- **L2 层**：中期上下文，经过轻量级压缩
- **L3 层**：长期记忆，采用摘要和关键信息提取
- 支持 `micro_compact`（微压缩）、`auto_compact`（自动压缩）和手动 `Compact` 三种模式，适配不同场景需求

### 结构化 Tracing 与可追查性
所有运行时事件均有记录，traces 输出为 `jsonl` 和可交互 `html` 两种格式。工具调用的输入输出完整保存，支持事后回溯分析——当 AI 的操作不符合预期时，你可以清晰定位问题所在。
- 事件流包含：用户输入、工具调用、模型响应、系统提示等全链路信息
- `html` 格式的 trace 提供可视化时间线，方便快速定位关键节点
- 支持跨会话 trace 对比，分析不同场景下的 AI 行为差异

### 统一工具协议
读、写、编辑、搜索、计划、任务管理、Bash 执行等所有工具遵循统一的响应协议。工具输出自动截断、落盘，并在需要时可回查完整内容，确保 AI 在工具使用上的可预测性和可审计性。
- 工具调用结果标准化，包含状态码、数据负载和错误信息
- 支持工具输出的分页和按需加载，避免上下文过长
- 统一的错误处理机制，提高 AI 工具使用的稳定性

### 灵活的 Session 管理
支持创建可命名、可选择、可恢复的会话。你可以为不同任务开启独立 session，也可以在任意时刻中断并后续接续。session 可绑定特定项目目录，也可以共享当前工作目录。
- 支持 `--workspace <path>` 显式绑定项目目录
- 未指定 workspace 时，默认绑定当前 shell 工作目录
- session 状态持久化，支持跨终端恢复和切换

### Skills 扩展机制
通过 `SkillLoader` 和 `Skill` 协议，xcode 支持动态加载自定义技能。目前内置了 code-review 和 repo-explore 两个 skill，你可以根据需要扩展更多场景。
- **code-review**：自动分析代码质量、潜在 bug 和性能问题
- **repo-explore**：快速扫描仓库结构，生成项目概览和关键模块说明
- 支持通过简单的配置文件定义新 skill，无需修改核心代码

## 应用场景

### 代码审查与重构
- 自动识别代码中的潜在问题：未使用的变量、死代码、性能瓶颈
- 提供重构建议，优化代码结构和可读性
- 生成代码变更的影响分析，评估重构风险

### 新项目快速上手
- 自动扫描仓库结构，生成项目概览和关键模块说明
- 提取核心功能和 API 文档，加速理解项目架构
- 针对特定模块提供详细解析，帮助快速融入开发

### 跨语言代码转换
- 支持将代码从一种语言转换为另一种语言（如 Python → TypeScript）
- 保持逻辑一致性的同时，适配目标语言的最佳实践
- 自动处理语言特性差异，减少手动调整

### 技术债务管理
- 识别代码库中的技术债务：重复代码、复杂逻辑、过时依赖
- 生成债务清理计划，按优先级排序
- 提供具体的重构步骤和代码示例

## 快速开始

### 安装与配置

```bash
# 克隆仓库
git clone <repository-url>
cd xcode

# 安装依赖（推荐使用 uv 或 pip）
pip install -e .

# 复制环境配置模板
cp .env.example .env
# 编辑 .env，填入你的 API 配置
# 例如：OPENAI_API_KEY=sk-xxx
```

### 基本使用

```bash
# 启动 CLI，默认绑定当前目录
python -m scripts.cli

# 绑定特定项目目录启动
python -m scripts.cli --workspace /path/to/your/project

# 创建新会话并指定名称
python -m scripts.cli --session my-feature-dev

# 恢复之前的会话
python -m scripts.cli --session existing-session-name
```

### 常用命令示例

#### 代码搜索与分析
```bash
# 在 CLI 中使用 Grep 工具搜索代码
Grep --pattern "def process" --path src/

# 查看文件内容
Read --file_path src/runtime/session.py

# 列出目录结构
LS --path src/
```

#### 代码编辑与修改
```bash
# 编辑文件
Edit --file_path src/tools/read_only.py --old_string "def ls" --new_string "def list_directory"

# 写入新文件
Write --file_path src/tools/new_tool.py --content "def new_function():\n    pass"
```

#### 任务管理
```bash
# 创建任务
TaskCreate --title "重构用户认证模块" --description "优化认证流程，提高安全性"

# 列出所有任务
TaskList

# 更新任务状态
TaskUpdate --task_id 1 --status in_progress
```

## 技术架构

xcode 采用模块化设计，核心组件包括：

```
src/
├── runtime/      # Agent 运行时：session 管理、runner、tracing
├── tools/        # 工具实现：读写编辑、搜索、任务管理、Bash 等
├── context/      # 上下文分层、压缩、@file 预处理
├── tasks/        # 任务图、子代理、后台执行、Agent Team
└── protocol/     # 统一工具响应协议
```

### 核心模块说明

- **runtime**：负责 Agent 的生命周期管理，包括会话创建、状态持久化和事件追踪。核心类包括 `Session`（会话管理）、`Runner`（Agent 执行器）和 `Tracing`（事件记录）。

- **context**：实现上下文的分层存储和压缩策略，确保长会话中上下文的有效性和相关性。关键组件包括 `Compaction`（上下文压缩）和 `ContextBuilder`（上下文构建）。

- **tools**：提供各类工具的实现，包括只读工具（LS、Glob、Grep、Read）、编辑工具（Edit、Write）、计划工具（TodoWrite）、任务管理工具和 Bash 执行工具。

- **tasks**：支持复杂任务的分解和执行，包括子代理（SubAgent）、后台执行（BackgroundRun）和 Agent Team 协作。

- **protocol**：定义统一的工具响应协议，确保所有工具的输出格式一致，便于 Agent 理解和处理。

## 产物与日志

### 运行时产物
- **traces**：保存在 `artifacts/traces/` 目录下，包含 `jsonl` 格式的完整事件流和 `html` 格式的可视化回放。
- **workspace 记忆**：存储在 `~/.xx-coding/projects/<project-key>/memory/` 目录，包含项目结构、关键代码和历史交互信息。
- **工具输出**：工具执行的完整输出会自动落盘，可通过回查命令获取。

### 日志级别
xcode 支持多级别日志，可在 `.env` 文件中配置：
- `LOG_LEVEL=DEBUG`：详细日志，包含所有工具调用和内部状态
- `LOG_LEVEL=INFO`：标准日志，记录关键操作和事件
- `LOG_LEVEL=ERROR`：仅记录错误信息

## 与其他工具的对比

| 特性 | xcode | 云端 Code Agent | 本地 IDE 插件 |
|------|-------|----------------|---------------|
| 本地执行 | ✅ | ❌ | ✅ |
| 长期记忆 | ✅ | ❌ | ❌ |
| 上下文治理 | ✅ | 有限 | 有限 |
| 可追查性 | ✅ | 有限 | ❌ |
| 自定义技能 | ✅ | 有限 | 有限 |
| 跨语言支持 | ✅ | ✅ | 依赖 IDE |

## 贡献指南

### 开发环境设置

```bash
# 安装开发依赖
pip install -e "[dev]"

# 运行测试
pytest tests/

# 代码风格检查
ruff check src/
```

### 扩展技能（Skills）

1. 在 `skills/` 目录下创建新的技能文件夹（如 `my-skill/`）
2. 在文件夹中创建 `SKILL.md` 文件，定义技能的名称、描述和参数
3. 实现技能逻辑（可选，可直接使用配置文件定义）
4. 在 CLI 中使用 `Skill` 工具加载和调用新技能

### 提交代码

- 遵循 PEP 8 代码风格
- 提交前运行测试和代码风格检查
- 提交信息清晰明了，说明修改内容和原因

## 未来规划

### 近期目标
- 增强代码生成的准确性和上下文相关性
- 支持更多编程语言和框架的特定分析
- 优化记忆存储和检索机制，提高长期记忆的效率
- 增加更多内置技能，如性能分析、安全审计等

### 中期目标
- 支持多模型集成，允许用户选择不同的 LLM 后端
- 实现更智能的任务分解和子代理协作
- 提供可视化的会话管理界面，增强用户体验
- 支持团队协作场景，共享项目记忆和会话

### 长期愿景
- 构建一个能够真正理解代码库的智能编程助手，成为开发者的得力搭档
- 实现代码库的自动维护和优化，减少手动工作量
- 支持从需求到部署的全流程辅助，覆盖软件开发的各个环节

---

xcode 仍在持续迭代中，欢迎提出问题和改进建议。如果你对项目感兴趣，欢迎参与贡献！