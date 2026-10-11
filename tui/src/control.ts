import {spawn, type ChildProcess} from 'node:child_process';

export type CommandController = {
	send: (packet: Record<string, unknown>) => void;
	cancel: () => void;
	dispose: () => void;
};

export function createController(child: ChildProcess): CommandController {
	let timer: ReturnType<typeof setTimeout> | undefined;
	let closed = false;
	const send = (packet: Record<string, unknown>): void => {
		if (!closed && child.stdin?.writable) child.stdin.write(`${JSON.stringify(packet)}\n`);
	};
	return {
		send,
		cancel: () => {
			if (closed || timer) return;
			send({type: 'cancel'});
			timer = setTimeout(() => {
				if (closed || !child.pid) return;
				if (process.platform === 'win32') {
					spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], {stdio: 'ignore'}).on('error', () => child.kill());
				} else {
					try { process.kill(-child.pid, 'SIGKILL'); } catch { child.kill('SIGKILL'); }
				}
			}, 5000);
			timer.unref();
		},
		dispose: () => { closed = true; if (timer) clearTimeout(timer); },
	};
}
