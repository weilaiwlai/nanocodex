# 运行时补强说明

## 已实现的五项

1. **统一权限与系统沙箱**：Bash、BackgroundRun、Grep 的外部进程和 Git 工作树操作共用执行器。Linux 使用 bubblewrap；macOS 使用 sandbox-exec。默认禁止命令联网，隔离进程环境和临时目录，保护 `.git`、会话状态和嵌套的 `.env*`、`.ssh`、`.pem`、`.key`。文件工具检查同一权限策略及符号链接真实路径。Windows 原生执行会明确报错，需要 WSL + bubblewrap。
2. **可控长命令**：实时 stdout/stderr、进程 ID、增量游标、持久日志、有限超时，以及终止进程树。前台默认 120 秒，上限 600 秒；后台使用相同限制。内存保留每个输出流最后 65,536 个字符；日志上限约 32 MiB，发生截断会标记。最多同时运行 8 条命令。
3. **任务一致性**：SQLite WAL 事务用于任务创建、认领、依赖更新和结果写回；自动迁移旧 `task_*.json`，保留原文件。持续心跳、唯一租约标识、任务版本检查阻止重复认领和过期结果覆盖；依赖循环会回滚。启动恢复只标记中断的后台命令，不破坏普通任务租约。
4. **上下文治理**：主代理与队友复用规则、记忆、历史构建和模型调用前预算检查。预算包含指令、工具 schema、历史、当前输入和输出预留；大工具输出归档并压缩，旧历史可生成摘要，当前用户请求保持原样。无法满足预算时明确报错。历史替换使用单个 SQLite 事务和版本检查，兼容旧 SDK 会话数据库。
5. **回归与评测**：`tests/core` 覆盖权限、并发、租约、历史、取消、工具接入和 SDK 适配层；`evals/coding_tasks.json` 提供 8 个独立编码任务，`scripts/evaluate.py` 支持重复执行和独立验收，输出 JSON 报告及提交文件。

## 权限和取消

模型通过 `RequestPermissions` 申请额外权限。CLI 显示具体路径和联网请求；TUI 使用 Y/N 回复。60 秒无回复或运行取消视为未批准。授权只在当前进程会话有效，并由该会话的队友共享。

显式启动授权示例：

```bash
uv run python scripts/cli.py --new-session --workspace /path/to/repo --allow-network "检查项目"
uv run python scripts/cli.py --allow-read /path/to/reference --allow-write /path/to/repo/.git "查看修改"
```

对工作区外的新目录，应授权其已存在的父目录。工作树创建需要批准仓库 `.git` 和目标工作树父目录写入。

CLI 在运行时按 Ctrl-C 取消当前运行并继续会话；空闲时 Ctrl-C/Ctrl-D 退出。TUI 在运行时按 Esc/Ctrl-C 取消；输入并提交后续请求会先取消当前运行，再执行后续请求。空闲时 Esc+Enter 换行，`/quit` 退出。

前后台工具返回 `process_id`；`CommandPoll(process_id, after)` 查询状态与增量输出，`CommandCancel(process_id)` 取消。命令会话不跨进程存活；CLI/TUI 进程关闭时会终止它们。

TUI 与 CLI 使用 JSONL：

```json
{"type":"cancel"}
{"type":"permission_response","request_id":"请求中的 ID","approved":false}
```

Python 入口为 `--json-events --control-stdin`；事件含 `command_started`、`command_output`、`command_finished`、`permission_requested`、`permission_resolved`、`context_compacted`。stdin 关闭会取消运行。TUI 优先使用 `NANOCODEX_TUI_PYTHON`，同时兼容旧的 `NANOCODOX_TUI_PYTHON`。

## 多 Agent 工作流

`WorkflowDefine` 保存一个可复用的 DAG 定义，`WorkflowStart` 启动新运行，`WorkflowStatus` 查看每个步骤和结果，`WorkflowList` 列出定义。启动时会为缺少的角色创建持久 teammate，之后队员自动认领已解锁任务；调度与步骤交接由任务运行时完成，不需要主 Agent逐步派发。

工作流 JSON 的结构如下：

```json
{
  "name": "implement-test-review",
  "steps": [
    {
      "step_id": "implement",
      "title": "Implement the change",
      "prompt": "Implement the requested change and summarize modified files.",
      "role": "coder",
      "require_worktree": true,
      "max_retries": 1
    },
    {
      "step_id": "test",
      "title": "Run focused tests",
      "prompt": "Run tests for the change and report failures.",
      "role": "tester",
      "depends_on": ["implement"]
    },
    {
      "step_id": "review",
      "title": "Review the result",
      "prompt": "Review the implementation and test result.",
      "role": "reviewer",
      "depends_on": ["implement", "test"],
      "input_from": ["implement", "test"]
    }
  ]
}
```

传给 `WorkflowDefine.workflow_json` 后，用返回的 `workflow_id` 调用 `WorkflowStart`。步骤可用 `role` 指定角色，也可用 `agent` 指定固定 teammate 名称；若两者都提供，认领时必须同时匹配。未填写 `input_from` 时，默认把全部 `depends_on` 步骤的摘要和产物引用交给下一步。工作流最多 32 步、8 条 Agent 路由；`max_retries` 范围为 0–5，默认 0。重试耗尽后运行标记为失败，下游保持阻塞，结果可通过 `WorkflowStatus` 检查。

若任一步设置 `require_worktree: true`，启动时会先从干净的主工作区创建一个共享分支和 worktree；所有步骤在该目录串行执行，保证后续步骤能读取前序文件改动。工作区有未提交改动时启动会失败，以免把当前改动排除在 worktree 外。运行成功后，`WorkflowCloseout` 支持 `keep`、`remove` 或 `merge`；`merge` 只会合并已完成且两边都干净的运行分支，且需要分支上已有提交。该工具不会替 Agent 自动提交文件。

评测任务从 3 个扩展到 8 个，覆盖列表稳定去重、范围夹取、时长格式化、CSV 引号解析和一次性迭代器平均值。固定答案 Agent 只验证评测 harness 和验收器能够工作，不代表真实模型的代码能力。

## 上下文配置

```dotenv
NANOCODEX_CONTEXT_WINDOW_TOKENS=32768
NANOCODEX_INPUT_BUDGET_TOKENS=24000
NANOCODEX_OUTPUT_RESERVE_TOKENS=4096
```

输入上限取 `INPUT_BUDGET` 与 `CONTEXT_WINDOW - OUTPUT_RESERVE` 中较小值；请根据实际模型配置。SDK 通过 `RunConfig.call_model_input_filter` 在每一次调用前治理输入，包含同一轮内的工具循环。自动及手动压缩原文保存在会话的 compaction 目录。token 计算是请求前估算；实际使用量以服务端和 SDK 回报为准。

`NANOCODEX_EXEC_ENV_ALLOW` 可由操作者指定额外传给命令的环境变量名，逗号分隔。默认不传 API key 和云凭据。此选项不应包含不需要的密钥。

## 验证命令

核心测试只使用 Python 标准库；缺少 SDK 时，适配层测试使用显式测试替身，不调用模型：

```bash
python3 -m unittest discover -s tests/core -v
python3 -m compileall -q src scripts
node scripts/test_tui_offline.mjs
```

最后一条需要 Node 24，覆盖纯输入模型和控制通道。安装 TUI 原有依赖后，还应运行 `npm --prefix tui run typecheck` 和 `npm --prefix tui test`，验证 React/Ink 界面。

评测流程自检（固定答案，不代表模型能力）：

```bash
python3 scripts/evaluate.py --agent-json '["python3","{project}/evals/smoke_agent.py"]' --repeat 2 --output artifacts/evals/smoke
```

真实 Nanocodex 评测，需要先安装项目已有依赖、配置有效 API，并在支持沙箱的系统运行：

```bash
python3 scripts/evaluate.py --agent-json '["/absolute/path/to/python","{project}/scripts/cli.py","--new-session","--workspace","{workspace}","--json-events","{prompt}"]' --repeat 3 --output artifacts/evals/nanocodex
```

可用同一任务集运行另一个 agent 的命令进行比较。验收脚本位于任务工作区外；评测程序执行的是操作者指定的可信命令和本地可信任务集。报告包括完成率、每项耗时、退出状态、权限请求数、工具调用数，以及 SDK 提供的主运行 token 统计。没有真实 usage 时 token 与费用为 `null`；提供 `--input-price`/`--output-price`（美元/百万 token）才计算费用。额外摘要和队友调用不计入这份主运行费用，不能将其当作完整账单。

## 当前验证范围

本次云环境缺少项目 Python/Node 依赖，离线缓存不足；因此 SDK 适配测试使用替身，React/Ink 完整类型检查和真实模型评测尚未执行。环境禁止 bubblewrap 创建命名空间，真实 OS 沙箱测试按原因跳过；程序保留失败即拒绝执行的行为。macOS 配置尚未在 macOS 实机验证。

GitHub Actions 在 Ubuntu 和 macOS 上运行核心测试、Python 编译检查及 TUI 离线测试；Linux job 安装 bubblewrap，macOS job 覆盖 `sandbox-exec` 集成测试。受 runner 内核或系统策略限制时，OS 沙箱集成测试会按明确原因跳过。

本轮增加的工作流测试和 8 项评测样例尚未在当前环境运行：仓库要求先进入 `conda xcode`，但该环境不可用。已运行的检查只有评测 JSON 结构校验和 TUI 离线测试（7 项通过）；推送后应以 Ubuntu/macOS CI 结果作为 Python 与沙箱检查依据。

已运行的核心测试、TUI 纯逻辑测试和固定答案评测用于检查实现及评测流程。8 个小任务用于建立可重复基线，不能据此判断与官方 Codex 的整体能力差距。
