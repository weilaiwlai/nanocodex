import React, {useEffect, useMemo, useState} from 'react';
import {Box, render, Text, useApp, useInput, useStdout} from 'ink';
import {spawn} from 'node:child_process';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

// Windows 中文环境下强制 UTF-8，避免边框和中文显示乱码
if (process.platform === 'win32') {
	// 尝试切换控制台代码页到 UTF-8 (65001)
	try {
		require('child_process').execSync('chcp 65001 >nul 2>&1', {stdio: 'ignore'});
	} catch {
		/* 忽略失败 */
	}
	if (process.stdout.isTTY) {
		try {
			process.stdout.setEncoding('utf8');
		} catch {
			/* 某些终端可能不支持 */
		}
	}
}

import {applyInputKey, getVisibleInputLines, type InputState} from './input_model.js';

type SessionDescriptor = {
	session_id: string;
	session_name: string;
	workspace_root: string;
};

type RuntimeEvent = {
	type: string;
	payload?: Record<string, unknown>;
};

type TimelineItem = {
	id: string;
	kind: 'user' | 'assistant' | 'activity';
	text: string;
};

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const REPO_ROOT = path.resolve(__dirname, '..', '..');

const THEME = {
	primary: 'cyan',
	secondary: 'gray',
	success: 'green',
	warning: 'yellow',
	error: 'red',
	user: 'yellow',
	assistant: 'green',
	activity: 'magenta',
	border: 'gray',
} as const;

function parseCliArgs(argv: string[]): {workspaceRoot?: string; sessionId?: string} {
	// TUI 第一版只解析两个最小参数：启动 workspace 和恢复 session。
	const args: {workspaceRoot?: string; sessionId?: string} = {};
	for (let index = 0; index < argv.length; index += 1) {
		const value = argv[index];
		if (value === '--workspace' && argv[index + 1]) {
			args.workspaceRoot = argv[index + 1];
			index += 1;
		}
		if (value === '--session' && argv[index + 1]) {
			args.sessionId = argv[index + 1];
			index += 1;
		}
	}
	return args;
}

function runPythonCli(
	args: string[],
	handlers: {
		onStdoutLine?: (line: string) => void;
		onStderrLine?: (line: string) => void;
	},
): Promise<void> {
	// Python CLI 仍然是真正的执行端；Ink 只负责把 JSONL 事件转成状态。
	// 支持 NANOCODOX_TUI_PYTHON 环境变量指定 python 路径（如 conda 环境）。
	// 例：NANOCODOX_TUI_PYTHON="C:\envs\nanocodex\python.exe"
	return new Promise((resolve, reject) => {
		const pythonOverride = process.env.NANOCODOX_TUI_PYTHON;
		const isWindows = process.platform === 'win32';
		const child = pythonOverride
			? spawn(pythonOverride, [path.join(REPO_ROOT, 'scripts/cli.py'), ...args], {
				cwd: REPO_ROOT,
				env: isWindows ? {...process.env, PYTHONIOENCODING: 'utf-8'} : process.env,
			})
			: spawn('uv', ['run', 'python', path.join(REPO_ROOT, 'scripts/cli.py'), ...args], {
				cwd: REPO_ROOT,
				env: isWindows ? {...process.env, PYTHONIOENCODING: 'utf-8'} : process.env,
			});
		let stdoutBuffer = '';
		let stderrBuffer = '';

		const flushLines = (
			buffer: string,
			handleLine: ((line: string) => void) | undefined,
		): string => {
			let remaining = buffer;
			while (remaining.includes('\n')) {
				const newlineIndex = remaining.indexOf('\n');
				const line = remaining.slice(0, newlineIndex).trim();
				remaining = remaining.slice(newlineIndex + 1);
				if (line && handleLine) {
					handleLine(line);
				}
			}
			return remaining;
		};

		child.stdout.on('data', (chunk: Buffer | string) => {
			stdoutBuffer += chunk.toString();
			stdoutBuffer = flushLines(stdoutBuffer, handlers.onStdoutLine);
		});

		child.stderr.on('data', (chunk: Buffer | string) => {
			stderrBuffer += chunk.toString();
			stderrBuffer = flushLines(stderrBuffer, handlers.onStderrLine);
		});

		child.on('error', reject);
		child.on('close', code => {
			const trailingStdout = stdoutBuffer.trim();
			if (trailingStdout && handlers.onStdoutLine) {
				handlers.onStdoutLine(trailingStdout);
			}
			const trailingStderr = stderrBuffer.trim();
			if (trailingStderr && handlers.onStderrLine) {
				handlers.onStderrLine(trailingStderr);
			}
			if (code === 0) {
				resolve();
				return;
			}
			reject(new Error(`Python CLI exited with code ${code ?? -1}`));
		});
	});
}

async function bootstrapSession(args: {workspaceRoot?: string; sessionId?: string}): Promise<SessionDescriptor> {
	// 会话初始化单独走一次 CLI，避免 TUI 自己生成 session id。
	const cliArgs = ['--print-session-json'];
	if (args.sessionId) {
		cliArgs.push('--session', args.sessionId);
	} else {
		cliArgs.push('--new-session');
		if (args.workspaceRoot) {
			cliArgs.push('--workspace', args.workspaceRoot);
		}
	}

	let descriptor: SessionDescriptor | null = null;
	await runPythonCli(cliArgs, {
		onStdoutLine: line => {
			descriptor = JSON.parse(line) as SessionDescriptor;
		},
	});
	if (descriptor === null) {
		throw new Error('无法初始化 session。');
	}
	return descriptor;
}

async function streamPrompt(
	sessionId: string,
	prompt: string,
	handlers: {
		onEvent: (event: RuntimeEvent) => void;
		onError: (message: string) => void;
	},
): Promise<void> {
	// 每轮 prompt 都复用同一个 session id，这样 TUI 虽是多次进程调用，状态仍然连续。
	await runPythonCli(['--session', sessionId, '--json-events', prompt], {
		onStdoutLine: line => {
			handlers.onEvent(JSON.parse(line) as RuntimeEvent);
		},
		onStderrLine: line => {
			handlers.onError(line);
		},
	});
}

function summarizeEvent(event: RuntimeEvent): string | null {
	// 右侧活动面板默认只展示摘要，不直接把大段 tool_result 或正文塞进去。
	const payload = event.payload ?? {};
	if (event.type === 'tool_started') {
		return `▶ ${String(payload.tool_name ?? 'unknown_tool')}`;
	}
	if (event.type === 'tool_result') {
		const summary = String(payload.summary ?? '').trim();
		return `✓ ${String(payload.tool_name ?? 'unknown_tool')}${summary ? ` · ${summary}` : ''}`;
	}
	if (event.type === 'background_result_arrived') {
		return `◆ ${String(payload.text ?? '')}`.trim();
	}
	if (event.type === 'team_message_arrived') {
		return `✉ ${String(payload.from ?? 'unknown')} → ${String(payload.to ?? 'unknown')}${payload.summary ? ` · ${String(payload.summary)}` : ''}`;
	}
	if (event.type === 'teammate_state_changed') {
		return `👤 ${String(payload.name ?? 'unknown')}: ${String(payload.previous_status ?? 'unknown')} → ${String(payload.status ?? 'unknown')}`;
	}
	return null;
}

function truncateForPanel(text: string, limit = 72): string {
	// 右侧 activity 先走单行摘要，避免长 JSON/长路径把整个面板撑坏。
	const normalized = text.replace(/\s+/g, ' ').trim();
	if (normalized.length <= limit) {
		return normalized;
	}
	return `${normalized.slice(0, Math.max(0, limit - 1))}…`;
}

function appendAssistantDelta(
	timeline: TimelineItem[],
	assistantId: string,
	delta: string,
): TimelineItem[] {
	// assistant 文本不能只假设自己永远位于时间线最后。
	// 一旦中间插入了 tool/team/background 事件，后续增量仍然应该回写到同一个 assistant turn。
	return timeline.map(item => {
		if (item.id !== assistantId || item.kind !== 'assistant') {
			return item;
		}
		return {
			...item,
			text: `${item.text}${delta}`,
		};
	});
}

function appendAssistantSegment(
	timeline: TimelineItem[],
	assistantId: string,
	text: string,
): TimelineItem[] {
	// assistant 文本按“连续输出段”组织。
	// 一旦中间穿插 tool/team/background 事件，下一段 assistant 文本就应该新开一条消息。
	const assistantSegment: TimelineItem = {id: assistantId, kind: 'assistant', text};
	return [
		...timeline,
		assistantSegment,
	].slice(-30);
}

function StatusDot({busy, status}: {busy: boolean; status: string}): React.ReactElement {
	const color = busy
		? 'yellow'
		: status.includes('失败')
			? 'red'
			: status === '就绪'
				? 'green'
				: 'cyan';
	const symbol = busy ? '◐' : '●';
	return (
		<Text color={color} bold>
			{symbol} {status}
		</Text>
	);
}

function Header({
	session,
	startupArgs,
	busy,
	status,
	errorText,
}: {
	session: SessionDescriptor | null;
	startupArgs: {workspaceRoot?: string; sessionId?: string};
	busy: boolean;
	status: string;
	errorText: string | null;
}): React.ReactElement {
	const workspace = session?.workspace_root ?? startupArgs.workspaceRoot ?? '默认工作区';
	return (
		<Box flexDirection="column" marginBottom={1}>
			<Box justifyContent="space-between" alignItems="center">
				<Text bold color={THEME.primary}>
					nanocodex
				</Text>
				<StatusDot busy={busy} status={status} />
			</Box>
			<Box>
				<Text dimColor wrap="wrap">
					{session?.session_name ?? '加载中...'}
					{session ? ` (${session.session_id})` : ''} · {workspace}
				</Text>
			</Box>
			{errorText ? (
				<Box marginTop={1}>
					<Text color={THEME.error} wrap="wrap">
						Error: {errorText}
					</Text>
				</Box>
			) : null}
		</Box>
	);
}

function sanitizeForTerminal(text: string): string {
	// 去掉回车符和可能干扰终端光标位置的控制字符，保留换行。
	return text.replace(/\r/g, '').replace(/[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F]/g, '');
}



function estimateRenderedLines(text: string, columns: number): number {
	// 粗略估算文本在终端中会占用多少行（ASCII 1 宽，CJK/全角 2 宽）。
	if (columns <= 0) {
		return 1;
	}

	const sanitized = sanitizeForTerminal(text);
	return sanitized.split('\n').reduce((total, line) => {
		const width = Array.from(line).reduce((sum, char) => {
			const code = char.charCodeAt(0);
			// CJK 统一表意文字及常见全角符号粗略按 2 宽处理。
			return sum + (code >= 0x2E80 ? 2 : 1);
		}, 0);
		return total + Math.max(1, Math.ceil(width / columns));
	}, 0);
}

function truncateToBottomLines(text: string, maxLines: number, columns: number): string {
	// 保留文本底部的 maxLines 个渲染行，用于长 assistant 消息在矮终端中显示最新内容。
	if (maxLines <= 0) {
		return '';
	}

	const sanitized = sanitizeForTerminal(text);
	const lines = sanitized.split('\n');
	let usedLines = 0;
	let startIndex = lines.length;
	for (let index = lines.length - 1; index >= 0; index -= 1) {
		usedLines += estimateRenderedLines(lines[index] ?? '', columns);
		if (usedLines > maxLines) {
			startIndex = index + 1;
			break;
		}

		startIndex = index;
	}

	const truncated = lines.slice(startIndex).join('\n');
	return truncated;
}

function TimelineItemView({item}: {item: TimelineItem}): React.ReactElement {
	const text = sanitizeForTerminal(item.text);
	if (item.kind === 'user') {
		return (
			<Box flexDirection="column" marginBottom={1} flexShrink={0}>
				<Text color={THEME.success} bold wrap="wrap">
					❯ {text}
				</Text>
			</Box>
		);
	}

	if (item.kind === 'assistant') {
		return (
			<Box flexDirection="column" marginBottom={1} flexShrink={0}>
				<Text wrap="wrap">{text || '…'}</Text>
			</Box>
		);
	}

	return (
		<Box marginBottom={1} flexShrink={0}>
			<Text color={THEME.activity} dimColor wrap="wrap">
				⎿ {text}
			</Text>
		</Box>
	);
}

function Timeline({
	timeline,
	height,
	columns,
	scrollOffset,
}: {
	timeline: TimelineItem[];
	height: number;
	columns: number;
	scrollOffset: number;
}): React.ReactElement {
	// 计算每条消息在终端中占用的行数（含间距）。
	const itemHeights = useMemo(
		() => timeline.map(item => estimateRenderedLines(item.text, columns) + 1),
		[timeline, columns],
	);
	const totalLines = itemHeights.reduce((sum, h) => sum + h, 0);
	const maxOffset = Math.max(0, totalLines - Math.max(3, height - 2));
	const offset = Math.min(scrollOffset, maxOffset);
	const needsScrollHint = maxOffset > 0 && offset < maxOffset;
	const availableLines = Math.max(3, height - 2 - (needsScrollHint ? 1 : 0));

	// 根据 offset（渲染行）从底部向上定位到起始消息和需要跳过的行数。
	let linesToSkip = offset;
	let startIndex = 0;
	let startLineSkip = 0;
	for (let index = timeline.length - 1; index >= 0; index -= 1) {
		const itemHeight = itemHeights[index] ?? 0;
		if (linesToSkip < itemHeight) {
			startIndex = index;
			startLineSkip = linesToSkip;
			break;
		}

		linesToSkip -= itemHeight;
	}

	// 从起始位置向下填充可见区域。
	const visibleItems: TimelineItem[] = [];
	let usedLines = 0;
	for (let index = startIndex; index < timeline.length; index += 1) {
		const item = timeline[index];
		if (!item) {
			break;
		}

		const contentLines = (itemHeights[index] ?? 1) - 1; // 去掉消息间距
		if (index === startIndex) {
			// 顶部被 offset 截断一部分，不再显示额外提示以节省行数。
			const keepFromBottom = Math.max(0, contentLines - startLineSkip);
			const canFit = availableLines - usedLines - 1;
			if (canFit <= 0) {
				break;
			}

			const take = Math.min(keepFromBottom, canFit);
			const truncated = truncateToBottomLines(item.text, take, columns);
			visibleItems.push({...item, text: truncated});
			usedLines += take + 1;
		} else if (usedLines + contentLines + 1 <= availableLines) {
			visibleItems.push(item);
			usedLines += contentLines + 1;
		} else {
			// 底部空间不足时截断该消息底部。
			const canFit = availableLines - usedLines - 1;
			if (canFit > 0) {
				const truncated = truncateToBottomLines(item.text, canFit, columns);
				visibleItems.push({...item, text: truncated});
			}

			break;
		}
	}

	const showScrollHint = needsScrollHint;
	return (
		<Box flexDirection="column" height={height} width="100%" overflow="hidden">
			{visibleItems.length === 0 ? (
				<Box flexGrow={1} justifyContent="center" alignItems="center">
					<Text color="gray" dimColor>
						暂无对话，在下方输入框开始提问
					</Text>
				</Box>
			) : (
				<Box flexDirection="column" flexGrow={1}>
					{visibleItems.map(item => <TimelineItemView key={item.id} item={item} />)}
				</Box>
			)}
			{showScrollHint ? (
				<Box marginTop={1}>
					<Text dimColor>⤉ 按 PgUp/PgDn 查看更多历史</Text>
				</Box>
			) : null}
		</Box>
	);
}

function Separator(): React.ReactElement {
	const {stdout} = useStdout();
	const width = Math.max(10, (stdout.columns ?? 80) - 2);
	return (
		<Box marginY={1}>
			<Text dimColor>{'─'.repeat(width)}</Text>
		</Box>
	);
}

function InputArea({
	inputState,
	busy,
}: {
	inputState: InputState;
	busy: boolean;
}): React.ReactElement {
	const inputLines = getVisibleInputLines(inputState.text);
	return (
		<Box flexDirection="column">
			<Box>
				<Text color={busy ? THEME.warning : THEME.success} bold>
					❯{' '}
				</Text>
				{inputState.text === '' ? (
					<Text color="gray" dimColor>
						输入 prompt，/quit 退出
					</Text>
				) : (
					<Box flexDirection="column">
						{inputLines.map((line, index) => (
							<Text key={`${index}-${line}`} wrap="wrap">
								{line}
								{index === inputLines.length - 1 ? '█' : ''}
							</Text>
						))}
					</Box>
				)}
			</Box>
			{inputState.pendingEscape ? (
				<Box marginTop={1}>
					<Text color={THEME.warning}>
						Esc 已按下：现在按 Enter 会插入换行。
					</Text>
				</Box>
			) : null}
		</Box>
	);
}

function Footer(): React.ReactElement {
	return (
		<Box marginTop={1}>
			<Text dimColor>
				Enter 发送 · Esc+Enter 换行 · PgUp/PgDn 滚动 · /quit 或 Ctrl+C 退出
			</Text>
		</Box>
	);
}

function App(): React.ReactElement {
	const {exit} = useApp();
	const {stdout} = useStdout();
	const startupArgs = useMemo(() => parseCliArgs(process.argv.slice(2)), []);
	const [session, setSession] = useState<SessionDescriptor | null>(null);
	const [inputState, setInputState] = useState<InputState>({text: '', pendingEscape: false});
	const [timeline, setTimeline] = useState<TimelineItem[]>([]);
	const [status, setStatus] = useState('正在初始化 session...');
	const [busy, setBusy] = useState(false);
	const [errorText, setErrorText] = useState<string | null>(null);
	const [scrollOffset, setScrollOffset] = useState(0);

	// 固定 Timeline 高度，避免消息过多时与输入区/页脚重叠。
	// header ~3 行、input 最多 ~8 行、footer ~1 行，再留 2 行 margin/border 余量。
	const timelineHeight = Math.max(8, (stdout.rows ?? 24) - 13);

	useInput((inputChunk, key) => {
		// 滚动独立于 busy 状态，允许在 assistant 运行时查看历史。
		// 每次滚动固定 3 行，避免步长随窗口高度变化导致的不规律感。
		const SCROLL_STEP = 3;
		if (key.pageUp) {
			setScrollOffset(previous => previous + SCROLL_STEP);
			return;
		}

		if (key.pageDown) {
			setScrollOffset(previous => Math.max(0, previous - SCROLL_STEP));
			return;
		}

		if (busy) {
			return;
		}

		// 输入编辑规则统一收口到独立模型里，组件层只负责消费结果。
		const action = applyInputKey(inputState, {inputChunk, key});
		if (action.kind === 'exit') {
			exit();
			return;
		}
		if (action.kind === 'submit') {
			setInputState(action.state);
			void submitPrompt(action.submittedText);
			return;
		}
		if (action.kind === 'update') {
			setInputState(action.state);
		}
	}, {isActive: true});

	useEffect(() => {
		// 新消息到来或用户开始输入时回到最新消息底部。
		setScrollOffset(0);
	}, [timeline.length, inputState.text]);

	useEffect(() => {
		// TUI 启动时先拿到 session 元信息，后续每轮 prompt 都复用它。
		let cancelled = false;
		void bootstrapSession(startupArgs)
			.then(descriptor => {
				if (cancelled) {
					return;
				}
				setSession(descriptor);
				setStatus('就绪');
			})
			.catch(error => {
				if (cancelled) {
					return;
				}
				setErrorText(error instanceof Error ? error.message : String(error));
				setStatus('初始化失败');
			});
		return () => {
			cancelled = true;
		};
	}, [startupArgs]);

	const appendTimeline = (kind: TimelineItem['kind'], text: string): void => {
		// 对话、工具活动、后台/team 事件都统一进一条时间线，UI 更接近聊天流。
		const nextText = kind === 'activity' ? truncateForPanel(text) : text;
		setTimeline(previous => [
			...previous,
			{id: `${Date.now()}-${previous.length}`, kind, text: nextText},
		].slice(-100));
	};

	const submitPrompt = async (prompt: string): Promise<void> => {
		// prompt 一提交就先清空输入框，再把 user turn 立即放进对话区。
		// 这样用户看到的是正常聊天流，而不是“输入框里还残留上一轮内容”。
		if (!session || busy) {
			return;
		}
		if (prompt.trim() === '/quit') {
			exit();
			return;
		}
		if (!prompt.trim()) {
			return;
		}

		setBusy(true);
		setErrorText(null);
		setStatus('正在运行...');
		appendTimeline('user', prompt);
		let currentAssistantId: string | null = null;
		let runFailedMessage: string | null = null;

		try {
			await streamPrompt(session.session_id, prompt, {
				onEvent: event => {
					if (event.type === 'assistant_text_delta') {
						const delta = String(event.payload?.delta ?? '');
						if (!delta) {
							return;
						}
						// assistant 文本首次出现时才创建消息块；如果之前被工具事件打断，就新开一段。
						if (currentAssistantId === null) {
							currentAssistantId = `assistant-${Date.now()}-${Math.random().toString(16).slice(2, 6)}`;
							const assistantId = currentAssistantId;
							setTimeline(previous => appendAssistantSegment(previous, assistantId, delta));
							return;
						}
						const assistantId = currentAssistantId;
						setTimeline(previous => appendAssistantDelta(previous, assistantId, delta));
						return;
					}
					if (event.type === 'run_failed') {
						runFailedMessage = String(event.payload?.message ?? '当前运行失败。');
						currentAssistantId = null;
						appendTimeline('activity', `[RunFailed] ${runFailedMessage}`);
						return;
					}

					const summary = summarizeEvent(event);
					if (summary) {
						// 一旦出现生命周期事件，后续 assistant 文本应该落到新的消息段里。
						currentAssistantId = null;
						appendTimeline('activity', summary);
					}
				},
				onError: message => {
					currentAssistantId = null;
					appendTimeline('activity', `[stderr] ${message}`);
				},
			});
			if (runFailedMessage) {
				setErrorText(runFailedMessage);
				setStatus('运行失败');
			} else {
				setStatus('就绪');
			}
		} catch (error) {
			const message = runFailedMessage ?? (error instanceof Error ? error.message : String(error));
			setErrorText(message);
			setStatus('运行失败');
		} finally {
			setBusy(false);
		}
	};

	return (
		<Box flexDirection="column" paddingX={1}>
			<Header
				session={session}
				startupArgs={startupArgs}
				busy={busy}
				status={status}
				errorText={errorText}
			/>
			<Timeline
				timeline={timeline}
				height={timelineHeight}
				columns={Math.max(10, stdout.columns - 2)}
				scrollOffset={scrollOffset}
			/>
			{busy ? (
				<Box marginBottom={1}>
					<Text color={THEME.warning}>✶ {status}</Text>
				</Box>
			) : null}
			<Separator />
			<InputArea inputState={inputState} busy={busy} />
			<Footer />
		</Box>
	);
}

export default App;

// Ink 程序必须显式调用 render() 才会真正进入交互循环。
// 这里只保留一个最小入口，不在启动层额外做复杂参数分发。
render(<App />);
