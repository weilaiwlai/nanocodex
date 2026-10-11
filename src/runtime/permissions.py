from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4


class PermissionDenied(RuntimeError):
    pass


class SandboxUnavailable(PermissionDenied):
    pass


def _within(path: Path, roots) -> bool:
    return any(path == root or root in path.parents for root in roots)


def protected_paths(roots):
    """Walk without following links; nested secrets get the same policy as Read."""
    found = []
    for root in roots:
        if not root.is_dir():
            if root.exists():
                found.append(root)
            continue
        for current, directories, files in os.walk(root, followlinks=False):
            base = Path(current)
            for name in list(directories):
                if name in {'.git', '.ssh'}:
                    found.append(base / name)
                    directories.remove(name)
            for name in files:
                if name == '.git' or (name.startswith('.env') and name != '.env.example') or Path(name).suffix in {'.pem', '.key'}:
                    found.append(base / name)
    return list(dict.fromkeys(found))


@dataclass
class PermissionBroker:
    emit: object = None
    handler: object = None
    timeout: float = 60
    read_roots: set[Path] = field(default_factory=set)
    write_roots: set[Path] = field(default_factory=set)
    network: bool = False
    _pending: dict = field(default_factory=dict)
    _lock: threading.Condition = field(default_factory=threading.Condition)
    _handler_lock: threading.Lock = field(default_factory=threading.Lock)

    def grant(self, *, read_paths=(), write_paths=(), network=False):
        with self._lock:
            self.read_roots.update(Path(p).expanduser().resolve() for p in read_paths)
            self.write_roots.update(Path(p).expanduser().resolve() for p in write_paths)
            self.network = self.network or network

    def snapshot(self):
        with self._lock:
            return tuple(self.read_roots | self.write_roots), tuple(self.write_roots), self.network

    def resolve(self, request_id: str, approved: bool) -> bool:
        with self._lock:
            request = self._pending.get(request_id)
            if request is None or request['decision'] is not None:
                return False
            request['decision'] = bool(approved)
            self._lock.notify_all()
            return True

    def request(self, *, read_paths=(), write_paths=(), network=False, reason='', cancelled=None):
        request_id = uuid4().hex
        payload = dict(request_id=request_id, read_paths=[str(Path(p).expanduser().resolve()) for p in read_paths],
                       write_paths=[str(Path(p).expanduser().resolve()) for p in write_paths],
                       network=bool(network), reason=reason, timeout_seconds=self.timeout)
        request = dict(payload, decision=None)
        with self._lock:
            self._pending[request_id] = request
        if self.emit:
            self.emit('permission_requested', payload)
        if self.handler:
            def handle():
                with self._handler_lock:
                    with self._lock:
                        if request_id not in self._pending or request['decision'] is not None:
                            return
                    try:
                        self.resolve(request_id, bool(self.handler(payload)))
                    except Exception:
                        self.resolve(request_id, False)
            threading.Thread(target=handle, daemon=True).start()
        deadline = time.monotonic() + self.timeout
        with self._lock:
            while request['decision'] is None and time.monotonic() < deadline:
                if cancelled is not None and cancelled.is_set():
                    break
                self._lock.wait(min(0.1, max(0, deadline - time.monotonic())))
            approved = request['decision'] is True and not (cancelled and cancelled.is_set())
            self._pending.pop(request_id, None)
        if approved:
            self.grant(read_paths=payload['read_paths'], write_paths=payload['write_paths'], network=network)
        if self.emit:
            self.emit('permission_resolved', dict(request_id=request_id, approved=approved))
        return approved


@dataclass(frozen=True)
class ExecutionPolicy:
    root: Path
    broker: PermissionBroker
    memory_roots: tuple[Path, ...] = ()
    state_roots: tuple[Path, ...] = ()

    def authorize_path(self, path: Path, *, write=False) -> Path:
        path = path.resolve()
        reads, writes, _ = self.broker.snapshot()
        approved = _within(path, writes if write else reads)
        roots = (self.root.resolve(), *self.memory_roots, *(writes if write else reads))
        if not _within(path, roots):
            raise PermissionDenied(f'路径超出允许范围: {path}')
        secret = any(part == '.ssh' for part in path.parts) or (path.name.startswith('.env') and path.name != '.env.example') or path.suffix in {'.pem', '.key'}
        protected_write = '.git' in path.parts or _within(path, self.state_roots)
        if (secret or (write and protected_write)) and not approved:
            raise PermissionDenied('该路径需要用户通过 RequestPermissions 显式授权。')
        return path

    def environment(self, scratch: Path) -> dict[str, str]:
        safe = {'PATH', 'LANG', 'TERM', 'COLORTERM', 'VIRTUAL_ENV', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
                'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy',
                'SYSTEMROOT', 'WINDIR'}
        safe.update(filter(None, os.getenv('NANOCODEX_EXEC_ENV_ALLOW', '').split(',')))
        env = {k: v for k, v in os.environ.items() if k in safe or k.startswith('LC_')}
        env.update(HOME=str(scratch), TMPDIR=str(scratch), GIT_TERMINAL_PROMPT='0', MYCODEAGENT='1')
        return env

    def command(self, command: str, cwd: Path, scratch: Path) -> list[str]:
        self.authorize_path(cwd)
        reads, writes, network = self.broker.snapshot()
        runtime_paths = [Path(p) for p in ('/usr', '/bin', '/sbin', '/lib', '/lib64', '/opt') if Path(p).exists()]
        runtime_paths += [Path(p) for p in ('/etc/ld.so.cache', '/etc/ssl', '/etc/resolv.conf', '/etc/hosts',
                                          '/etc/nsswitch.conf', '/etc/passwd', '/etc/group', '/etc/localtime') if Path(p).exists()]
        if sys.platform == 'darwin':
            runtime_paths += [Path(p) for p in ('/System', '/Library', '/private/var/db') if Path(p).exists()]
        certificate = os.environ.get('SSL_CERT_FILE')
        if certificate and Path(certificate).is_file():
            runtime_paths.append(Path(certificate))
        read_roots = [*runtime_paths, *reads]
        write_roots = [self.root.resolve(), scratch.resolve(), *writes]
        protected = protected_paths([self.root.resolve(), *writes])
        if sys.platform.startswith('linux'):
            binary = shutil.which('bwrap')
            if not binary:
                raise SandboxUnavailable('缺少 bubblewrap。请安装 bwrap；执行器不会降级为无沙箱运行。')
            args = [binary, '--die-with-parent', '--new-session', '--unshare-all', '--cap-drop', 'ALL']
            if network:
                args.append('--share-net')
            args += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
            for root in dict.fromkeys(read_roots):
                if root.exists():
                    args += ['--ro-bind', str(root), str(root)]
            for root in dict.fromkeys(write_roots):
                if not root.is_dir():
                    if root.exists():
                        args += ['--bind', str(root), str(root)]
                    continue
                args += ['--bind', str(root), str(root)]
            for root in self.state_roots:
                if root.is_dir() and _within(root, write_roots) and not _within(root, writes):
                    args += ['--ro-bind', str(root), str(root)]
                    for approved in writes:
                        if approved.exists() and _within(approved, (root,)):
                            args += ['--bind', str(approved), str(approved)]
            empty = scratch / 'empty'
            empty.mkdir(exist_ok=True)
            for path in protected:
                if path.name == '.git':
                    if not _within(path.resolve(), writes):
                        args += ['--ro-bind', str(path), str(path)]
                elif not _within(path.resolve(), reads):
                    args += ['--ro-bind', str(empty) if path.is_dir() else '/dev/null', str(path)]
            for root in reads:
                if root.is_file() and not _within(root, writes):
                    args += ['--ro-bind', str(root), str(root)]
            # The command's private home/temp remains writable beneath a read-only state root.
            args += ['--bind', str(scratch), str(scratch)]
            return [*args, '--chdir', str(cwd), '--', '/bin/bash', '--noprofile', '--norc', '-c', command]
        if sys.platform == 'darwin':
            binary = shutil.which('sandbox-exec')
            if not binary:
                raise SandboxUnavailable('sandbox-exec 不可用。')
            quote = lambda p: json.dumps(str(p))
            profile = '(version 1)(deny default)(allow process*)(allow sysctl-read)'
            for root in [*read_roots, *write_roots]:
                profile += f'(allow file-read* (subpath {quote(root)}))'
            for root in write_roots:
                profile += f'(allow file-write* (subpath {quote(root)}))'
            for path in protected:
                kind = 'subpath' if path.is_dir() else 'literal'
                if path.name == '.git':
                    if not _within(path.resolve(), writes):
                        profile += f'(deny file-write* ({kind} {quote(path)}))'
                elif not _within(path.resolve(), reads):
                    profile += f'(deny file-read* ({kind} {quote(path)}))(deny file-write* ({kind} {quote(path)}))'
            for root in self.state_roots:
                if root.is_dir() and not _within(root, writes):
                    profile += f'(deny file-write* (require-all (subpath {quote(root)}) (require-not (subpath {quote(scratch)}))))'
            if network:
                profile += '(allow network*)'
            return [binary, '-p', profile, '/bin/bash', '--noprofile', '--norc', '-c', command]
        raise SandboxUnavailable('当前平台没有受支持的原生沙箱；请使用 Linux/WSL + bubblewrap。')
