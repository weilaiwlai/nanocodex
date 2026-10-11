"""Run repeatable coding tasks against an explicitly selected agent command."""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def stop_process(process):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=5)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def run_command(argv, *, cwd, timeout, log_path):
    with log_path.open('w', encoding='utf-8') as output:
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
            stop_process(process)
            return code, False
        except subprocess.TimeoutExpired:
            stop_process(process)
            return process.returncode, True


def parse_usage(path):
    usage = None
    interventions = 0
    tools = 0
    with path.open(encoding='utf-8', errors='replace') as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'run_finished':
                reported = event.get('payload', {}).get('usage', {})
                if reported and all(isinstance(reported.get(k), int) for k in ('prompt_tokens', 'completion_tokens')):
                    usage = reported
            interventions += event.get('type') == 'permission_requested'
            tools += event.get('type') == 'tool_started'
    return usage, interventions, tools


def evaluate(*, tasks_path, agent_argv, output_dir, timeout=120, repeat=1, input_price=None, output_price=None):
    tasks = json.loads(Path(tasks_path).read_text(encoding='utf-8'))
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for iteration in range(repeat):
        for index, task in enumerate(tasks):
            label = f'{index + 1:03d}-{iteration + 1}'
            log_dir = output_dir / label
            log_dir.mkdir(exist_ok=True)
            started = time.monotonic()
            with tempfile.TemporaryDirectory(prefix='nanocodex-eval-') as temporary:
                workspace = Path(temporary) / 'workspace'
                workspace.mkdir()
                for name, text in task['files'].items():
                    destination = (workspace / name).resolve()
                    if workspace not in destination.parents:
                        raise ValueError('fixture 文件路径必须位于 workspace 内。')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(text, encoding='utf-8')
                # The verifier lives outside the agent workspace and is never passed to it.
                verifier = Path(temporary) / 'verify.py'
                verifier.write_text(task['verifier'], encoding='utf-8')
                values = {'workspace': str(workspace), 'prompt': task['prompt'], 'project': str(ROOT)}
                argv = [re.sub(r'\{(workspace|prompt|project)\}', lambda match: values[match[1]], part) for part in agent_argv]
                try:
                    code, timed_out = run_command(argv, cwd=workspace, timeout=timeout, log_path=log_dir / 'agent.log')
                    verify_code, verify_timeout = run_command([sys.executable, '-I', str(verifier), str(workspace)],
                                                              cwd=Path(temporary), timeout=10, log_path=log_dir / 'verify.log')
                    usage, interventions, tools = parse_usage(log_dir / 'agent.log')
                    cost = None
                    if usage is not None and input_price is not None and output_price is not None:
                        cost = (usage['prompt_tokens'] * input_price + usage['completion_tokens'] * output_price) / 1_000_000
                    result = dict(task_id=task['id'], iteration=iteration + 1,
                                  passed=code == 0 and verify_code == 0 and not timed_out and not verify_timeout,
                                  agent_exit_code=code, verifier_exit_code=verify_code, timed_out=timed_out,
                                  verifier_timed_out=verify_timeout, tokens=usage, cost_usd=cost,
                                  permission_requests=interventions, tool_calls=tools)
                except OSError as exc:
                    result = dict(task_id=task['id'], iteration=iteration + 1, passed=False, error=str(exc),
                                  tokens=None, cost_usd=None, permission_requests=0, tool_calls=0)
                # Preserve submitted edits for review, without copying symlinks outside the workspace.
                submitted = log_dir / 'submitted'
                submitted.mkdir(exist_ok=True)
                for file in workspace.rglob('*'):
                    if file.is_file() and not file.is_symlink():
                        target = submitted / file.relative_to(workspace)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(file.read_bytes())
            result['elapsed_seconds'] = round(time.monotonic() - started, 3)
            results.append(result)
            print(f'{task["id"]} #{iteration + 1}: {"PASS" if result["passed"] else "FAIL"}', flush=True)
    report = dict(created_at=datetime.now(UTC).isoformat(), agent_argv=agent_argv,
                  tasks_path=str(Path(tasks_path).resolve()), repeat=repeat, total=len(results),
                  passed=sum(r['passed'] for r in results),
                  completion_rate=sum(r['passed'] for r in results) / len(results) if results else 0,
                  results=results)
    (output_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tasks', type=Path, default=ROOT / 'evals' / 'coding_tasks.json')
    parser.add_argument('--agent-json', required=True, help='JSON argv array with {workspace}, {prompt}, {project} placeholders.')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts' / 'evals' / datetime.now().strftime('%Y%m%d-%H%M%S'))
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--input-price', type=float, help='USD per million input tokens.')
    parser.add_argument('--output-price', type=float, help='USD per million output tokens.')
    args = parser.parse_args()
    argv = json.loads(args.agent_json)
    if not isinstance(argv, list) or not argv or not all(isinstance(p, str) for p in argv) or args.timeout <= 0 or args.repeat < 1:
        parser.error('Invalid argv, timeout or repeat.')
    report = evaluate(tasks_path=args.tasks, agent_argv=argv, output_dir=args.output,
                      timeout=args.timeout, repeat=args.repeat, input_price=args.input_price, output_price=args.output_price)
    print(f'{report["passed"]}/{report["total"]} passed; report: {args.output / "report.json"}')
    return 0 if report['passed'] == report['total'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
