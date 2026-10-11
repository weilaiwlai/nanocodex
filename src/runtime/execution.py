from __future__ import annotations

import codecs
import os
import signal
import subprocess
import tempfile
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from src.runtime.permissions import ExecutionPolicy


@dataclass
class CommandSession:
    id: str
    process: subprocess.Popen
    command: str
    cwd: Path
    log_path: Path
    owner: str
    timeout: float
    started: float = field(default_factory=time.monotonic)
    status: str = 'running'
    exit_code: int | None = None
    stdout: str = ''
    stderr: str = ''
    sequence: int = 0
    events: deque = field(default_factory=lambda: deque(maxlen=256))
    lock: threading.RLock = field(default_factory=threading.RLock)
    done: threading.Event = field(default_factory=threading.Event)
    scratch: Path | None = None
    finished: float | None = None
    truncated: bool = False
    log_bytes: int = 0


class CommandManager:
    def __init__(self):
        self._sessions: dict[str, CommandSession] = {}
        self._lock = threading.RLock()

    def start(self, command: str, *, cwd: Path, policy: ExecutionPolicy,
              output_dir: Path, timeout: float = 120, owner: str = 'team-lead', emit=None) -> CommandSession:
        if not command.strip() or not 0 < timeout <= 600:
            raise ValueError('command 不能为空，timeout 必须在 0 到 600 秒之间。')
        cwd = cwd.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        process_id = uuid4().hex
        scratch = Path(tempfile.mkdtemp(prefix='sandbox-', dir=output_dir))
        try:
            argv = policy.command(command, cwd, scratch)
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        with self._lock:
            if sum(not item.done.is_set() for item in self._sessions.values()) >= 8:
                shutil.rmtree(scratch, ignore_errors=True)
                raise RuntimeError('并行命令已达到上限 8。')
            try:
                process = subprocess.Popen(argv, cwd=cwd, env=policy.environment(scratch),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           start_new_session=True, close_fds=True)
            except BaseException:
                shutil.rmtree(scratch, ignore_errors=True)
                raise
            session = CommandSession(process_id, process, command, cwd, output_dir / f'{process_id}.log', owner, timeout)
            session.scratch = scratch
            session.log_path.touch(mode=0o600)
            # A returned tool artifact is readable without asking for unrelated paths.
            if isinstance(policy, ExecutionPolicy):
                policy.broker.grant(read_paths=[session.log_path])
            completed = [key for key, item in self._sessions.items() if item.done.is_set()]
            for key in completed[:-63]:
                del self._sessions[key]
            self._sessions[process_id] = session
        if emit:
            emit('command_started', dict(process_id=process_id, command=command, owner=owner))
        readers = [threading.Thread(target=self._read, args=(session, stream, name, emit), daemon=True)
                   for stream, name in ((process.stdout, 'stdout'), (process.stderr, 'stderr'))]
        for reader in readers:
            reader.start()
        threading.Thread(target=self._watch, args=(session, readers, emit), daemon=True).start()
        return session

    def _read(self, session, stream, name, emit):
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        try:
            while True:
                raw = os.read(stream.fileno(), 4096)
                text = decoder.decode(raw, final=not raw)
                if text:
                    with session.lock:
                        session.sequence += 1
                        event = dict(sequence=session.sequence, stream=name, text=text)
                        session.events.append(event)
                        value = getattr(session, name) + text
                        session.truncated |= len(value) > 65536
                        setattr(session, name, value[-65536:])
                        if session.log_bytes < 32 * 1024 * 1024:
                            with session.log_path.open('a', encoding='utf-8') as log:
                                log.write(f'[{name}] {text}')
                            session.log_bytes += len(text.encode('utf-8'))
                        else:
                            session.truncated = True
                    if emit:
                        emit('command_output', dict(process_id=session.id, **event))
                if not raw:
                    break
        finally:
            stream.close()

    def _watch(self, session, readers, emit):
        while session.process.poll() is None:
            if time.monotonic() - session.started >= session.timeout:
                self.cancel(session.id, status='timed_out')
                break
            time.sleep(0.05)
        session.exit_code = session.process.wait()
        # A shell may exit while descendants still hold its pipes. Reap its group.
        try:
            os.killpg(session.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for reader in readers:
            reader.join(timeout=2)
        with session.lock:
            session.finished = time.monotonic()
            if session.status == 'running':
                session.status = 'completed' if session.exit_code == 0 else 'failed'
            session.done.set()
        if session.scratch:
            shutil.rmtree(session.scratch, ignore_errors=True)
        if emit:
            emit('command_finished', dict(process_id=session.id, status=session.status, exit_code=session.exit_code))

    def get(self, process_id: str) -> CommandSession:
        with self._lock:
            if process_id not in self._sessions:
                raise KeyError(f'未知命令会话: {process_id}')
            return self._sessions[process_id]

    def poll(self, process_id: str, *, after: int = 0) -> dict:
        session = self.get(process_id)
        with session.lock:
            events = [dict(item) for item in session.events if item['sequence'] > after]
            return dict(process_id=session.id, status=session.status, exit_code=session.exit_code,
                        stdout=session.stdout, stderr=session.stderr, events=events, cursor=session.sequence,
                        output_truncated=session.truncated or bool(session.events and after < session.events[0]['sequence'] - 1),
                        log_path=str(session.log_path), elapsed_ms=int(((session.finished or time.monotonic()) - session.started) * 1000))

    def wait(self, process_id: str, *, cancelled=None) -> dict:
        session = self.get(process_id)
        while not session.done.wait(0.05):
            if cancelled is not None and cancelled.is_set():
                self.cancel(process_id)
        return self.poll(process_id)

    def cancel(self, process_id: str, *, status: str = 'cancelled') -> dict:
        session = self.get(process_id)
        with session.lock:
            if session.done.is_set():
                return self.poll(process_id)
            session.status = status
            try:
                os.killpg(session.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            session.process.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(session.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return self.poll(process_id)

    def cancel_owner(self, owner: str | None = None):
        with self._lock:
            ids = [item.id for item in self._sessions.values() if not item.done.is_set() and (owner is None or item.owner == owner)]
        for process_id in ids:
            self.cancel(process_id)

    def close(self):
        self.cancel_owner()
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.done.wait(3)
