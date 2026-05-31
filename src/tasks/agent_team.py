from __future__ import annotations

import asyncio
import json
import queue
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agents import Agent, Runner, SQLiteSession

from src.runtime.session import ToolRuntimeContext
from src.runtime.tracing import build_trace_logger
from src.tasks.task_graph import claim_task as claim_persistent_task, renew_task_lease, update_task
from src.tasks.task_store import get_task
from src.tasks.worktrees import ensure_task_worktree
from src.tools.common import ToolFailure
from src.tools.read_only import READ_ONLY_TOOLS
from src.tools.edit_write import FILE_EDIT_TOOLS
from src.tools.bash_tool import BASH_TOOLS
from src.tools.task_tools import TASK_TOOLS

# 这个模块覆盖 AgentTeam phase 1 到 phase 4 的闭环：
# team-lead、长寿命 teammate、独立 SQLiteSession、内存消息队列、transcript，
# phase 2 的请求协议、phase 3 的 task claim / lease / heartbeat，
# 以及 phase 4 的独立 session 和 worktree 绑定。
TEAM_TASK_LEASE_SECONDS = 30


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _build_default_team_state(*, session_id: str, session_name: str) -> dict[str, object]:
    now = _utc_now()
    # team_id 直接稳定绑定 session，team_name 允许自定义覆盖。
    return {
        "team_id": f"team-{session_id}",
        "team_name": f"{session_name} Team",
        "lead_name": "team-lead",
        "lead_session_id": session_id,
        "members": [],
        "created_at": now,
        "updated_at": now,
    }


def _normalize_member_status(member: dict[str, object]) -> bool:
    # 重启后要把旧活动状态诚实改成 stopped，后续由恢复逻辑决定是否重新 spawn。
    status = str(member.get("status") or "")
    if status in {"spawning", "working", "idle", "stopping"}:
        member["status"] = "stopped"
        return True
    return False


def _generate_span_id() -> str:
    return f"span-{uuid4().hex[:10]}"


def _build_transcript_event(
    *,
    event_type: str,
    payload: dict[str, object],
    span_id: str | None = None,
    parent_span_id: str | None = None,
    duration_ms: int | None = None,
) -> dict[str, object]:
    # transcript event 支持 span 结构，用于追踪 agent run 和 tool call 的层级关系。
    event: dict[str, object] = {
        "event_type": event_type,
        "created_at": _utc_now(),
        **payload,
    }
    if span_id is not None:
        event["span_id"] = span_id
    if parent_span_id is not None:
        event["parent_span_id"] = parent_span_id
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
    return event


def _write_json_file(path: Path, payload: dict[str, object]) -> None:
    # team_state 和 request_tracker 会被多线程快速读写。
    # 这里用同目录临时文件再 replace，避免读到半截 JSON。
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


def _build_message_input(message: dict[str, object]) -> dict[str, str]:
    # teammate 收到消息后，把结构化信封压成一条 user/message 视图送给模型。
    parts = [
        f"来自 {message['from']} 的团队消息",
        f"type: {message['type']}",
    ]
    summary = str(message.get("summary") or "").strip()
    if summary:
        parts.append(f"summary: {summary}")
    request_id = str(message.get("request_id") or "").strip()
    if request_id:
        parts.append(f"request_id: {request_id}")
    request_status = str(message.get("request_status") or "").strip()
    if request_status:
        parts.append(f"status: {request_status}")
    content = str(message.get("content") or "").strip()
    if content:
        parts.append("")
        parts.append(content)
    return {
        "role": "user",
        "content": "\n".join(parts).strip(),
    }


def _build_task_input(task: dict[str, object]) -> dict[str, str]:
    # task_assignment 不走普通消息摘要，而是直接告诉 teammate 当前认领到的任务。
    parts = [
        "你刚刚认领到了一个任务。",
        "type: task_assignment",
        f"task_id: {task['id']}",
        f"title: {task['title']}",
    ]
    summary = str(task.get("summary") or "").strip()
    if summary:
        parts.append(f"summary: {summary}")
    prompt = str(task.get("prompt") or "").strip()
    if prompt:
        parts.extend(["", prompt])
    return {
        "role": "user",
        "content": "\n".join(parts).strip(),
    }


def _build_teammate_identity_input(
    *,
    team_id: str,
    worker: "TeammateWorker",
    current_task_id: int | None,
) -> dict[str, str]:
    # 这层 stable reinjection 用来告诉 teammate"我是谁、当前归属什么任务"。
    # 它不依赖历史 transcript，所以不会被 compact 吞掉。
    current_task_text = str(current_task_id) if current_task_id is not None else "none"
    content = "\n".join(
        [
            "<teammate-identity>",
            f"name: {worker.name}",
            f"agent_id: {worker.agent_id}",
            f"role: {worker.role}",
            f"team_id: {team_id}",
            f"parent_session_id: {worker.context.session_id}",
            f"current_task_id: {current_task_text}",
            "tools: file tools, shell tools, task tools, SendMessage, ClaimTask, Idle, ShutdownResponse, PlanApproval",
            "</teammate-identity>",
        ]
    )
    return {
        "role": "system",
        "content": content,
    }


def _build_teammate_instructions(*, name: str, role: str) -> str:
    sections = [
        f"You are teammate '{name}'.",
        f"Your role is: {role}.",
        "You are a long-lived worker inside the current team.",
        "",
        "When you receive a task or message, you MUST use the available tools to complete it:",
        "- Use Glob to list files and directories",
        "- Use Read to read file contents",
        "- Use Grep to search for patterns in code",
        "- Use Bash to run shell commands when needed",
        "- Use TaskCreate / TaskList / TaskGet / TaskUpdate to manage persistent tasks",
        "",
        "After completing your work, report results back to team-lead using SendMessage.",
        "If there is no direct message, claim an available task from the task board using ClaimTask.",
        "If you need lead to review a plan, use PlanApproval in request mode.",
        "Do not spawn new teammates.",
        "Never return an empty response. Always produce meaningful output.",
    ]
    return "\n".join(sections).strip()


def _build_teammate_tools():
    # teammate 工具集先复用现有工作工具，再补最小 team phase 2 工具。
    from src.tools.team_tools import (
        claim_task_tool,
        idle_tool,
        plan_approval_tool,
        send_message_tool,
        shutdown_response_tool,
    )

    return [
        *READ_ONLY_TOOLS,
        *FILE_EDIT_TOOLS,
        *BASH_TOOLS,
        *TASK_TOOLS,
        send_message_tool,
        claim_task_tool,
        plan_approval_tool,
        shutdown_response_tool,
        idle_tool,
    ]


@dataclass(slots=True)
class TeammateWorker:
    # 这个对象只保存单个 teammate 的进程内运行态。
    # 持久化状态仍然写回 team_state.json 和 transcript。
    agent_id: str
    name: str
    role: str
    prompt: str
    transcript_path: Path
    recent_transcript_path: Path
    context: ToolRuntimeContext
    sdk_session: SQLiteSession
    message_queue: queue.Queue[dict[str, object]]
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class AgentTeamRuntime:
    def __init__(
        self,
        *,
        session_id: str,
        session_name: str,
        team_dir: Path,
        base_context: ToolRuntimeContext,
        team_name: str | None = None,
    ) -> None:
        # team runtime 绑定到一个 session，下层所有 teammate 都共享这套根目录。
        self.session_id = session_id
        self.session_name = session_name
        self.team_dir = team_dir
        self.base_context = base_context
        self._custom_team_name = team_name
        self.transcripts_dir = self.team_dir / "transcripts"
        self.sessions_dir = self.team_dir / "sessions"
        self.state_path = self.team_dir / "team_state.json"
        self.request_tracker_path = self.team_dir / "request_tracker.json"
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._lead_queue: queue.Queue[dict[str, object]] = queue.Queue()
        self._lead_state_queue: queue.Queue[dict[str, object]] = queue.Queue()
        self._workers: dict[str, TeammateWorker] = {}
        self._state = self._load_or_create_state()
        self._request_tracker = self._load_or_create_request_tracker()

    def _load_or_create_state(self) -> dict[str, object]:
        # 如果 team_state 已存在，说明这是同一 session 的恢复流程。
        # 这时要把旧进程遗留的活动状态统一收敛成 stopped。
        self.team_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            state = _build_default_team_state(
                session_id=self.session_id,
                session_name=self.session_name,
            )
            if self._custom_team_name:
                state["team_name"] = self._custom_team_name
            self._save_state(state)
            return state

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        changed = False
        # 允许自定义 team_name 覆盖持久化的值。
        if self._custom_team_name and state.get("team_name") != self._custom_team_name:
            state["team_name"] = self._custom_team_name
            changed = True
        for member in state.get("members", []):
            changed = _normalize_member_status(member) or changed
        if changed:
            state["updated_at"] = _utc_now()
            self._save_state(state)
        return state

    def _save_state(self, state: dict[str, object]) -> None:
        # 保持直接的 JSON 落盘，不额外引入 store 抽象。
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_file(self.state_path, state)

    def _load_or_create_request_tracker(self) -> dict[str, dict[str, object]]:
        # tracker 单独落盘，避免把 team_state 和请求流状态混在同一个文件里。
        if not self.request_tracker_path.exists():
            self._save_request_tracker({})
            return {}
        raw = json.loads(self.request_tracker_path.read_text(encoding="utf-8"))
        return {str(key): value for key, value in raw.items()}

    def _save_request_tracker(self, tracker: dict[str, dict[str, object]]) -> None:
        self.request_tracker_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_file(self.request_tracker_path, tracker)

    def _recover_teammates(self) -> None:
        # 从 team_state.json 恢复上次会话的 teammate。
        # 只恢复有未完成 task 或标记为 persistent 的 member。
        recoverable = []
        with self._lock:
            for member in self._state["members"]:
                status = str(member.get("status") or "")
                if status != "stopped":
                    continue
                if member.get("current_task_id") is not None or member.get("persistent"):
                    recoverable.append(dict(member))
                    member["status"] = "recovering"
            if recoverable:
                self._state["updated_at"] = _utc_now()
                self._save_state(self._state)

        for member_info in recoverable:
            try:
                self._respawn_from_member(member_info)
            except Exception as exc:
                # 单个 teammate 恢复失败不影响其他 teammate。
                name = str(member_info.get("name") or member_info.get("agent_id") or "unknown")
                try:
                    self._update_member(name, status="failed")
                except Exception:
                    pass

    def _respawn_from_member(self, member_info: dict[str, object]) -> None:
        # 用已有 member 信息重新创建 worker 并启动线程。
        # SQLiteSession 用相同的 session_id 打开同一 DB 文件，自动恢复对话历史。
        agent_id = str(member_info["agent_id"])
        name = str(member_info["name"])
        role = str(member_info.get("role") or "")
        prompt = str(member_info.get("prompt") or "")
        transcript_path = Path(str(member_info["transcript_path"]))
        if not transcript_path.is_absolute():
            transcript_path = self.base_context.session_dir / transcript_path
        recent_transcript_path = transcript_path.with_name(f"{agent_id}_recent.json")

        teammate_session = SQLiteSession(
            session_id=agent_id,
            db_path=self.sessions_dir / f"{agent_id}.db",
        )
        worker = TeammateWorker(
            agent_id=agent_id,
            name=name,
            role=role,
            prompt=prompt,
            transcript_path=transcript_path,
            recent_transcript_path=recent_transcript_path,
            context=self._build_worker_context(name=name, agent_id=agent_id, sdk_session=teammate_session),
            sdk_session=teammate_session,
            message_queue=queue.Queue(),
        )
        thread = threading.Thread(
            target=self._run_worker_loop,
            args=(worker,),
            daemon=True,
            name=f"teammate-{name}",
        )
        worker.thread = thread
        self._workers[name] = worker
        thread.start()
        self.send_message(
            from_name="team-lead",
            to_name=name,
            content=f"会话恢复：teammate '{name}' 已重新上线。",
            summary="teammate 恢复通知",
            message_type="message",
        )

    def _find_member(self, name: str) -> dict[str, object] | None:
        for member in self._state["members"]:
            if member["name"] == name:
                return member
        return None

    def _update_member(self, name: str, **updates: object) -> dict[str, object]:
        # 所有状态切换都统一走这一层，避免线程间把 team_state 写散。
        with self._lock:
            member = self._find_member(name)
            if member is None:
                raise ToolFailure(
                    code="NOT_FOUND",
                    message=f"未找到 teammate: {name}",
                    text=f"未找到 teammate '{name}'。",
                )
            previous_status = member.get("status")
            previous_task_id = member.get("current_task_id")
            previous_worktree = member.get("current_worktree")
            member.update(updates)
            self._state["updated_at"] = _utc_now()
            self._save_state(self._state)
            if (
                member.get("status") != previous_status
                or member.get("current_task_id") != previous_task_id
                or member.get("current_worktree") != previous_worktree
            ):
                self._lead_state_queue.put(
                    {
                        "name": name,
                        "previous_status": previous_status,
                        "status": member.get("status"),
                        "current_task_id": member.get("current_task_id"),
                        "current_worktree": member.get("current_worktree"),
                    }
                )
            return dict(member)

    def _append_transcript_event(self, worker: TeammateWorker, event: dict[str, object]) -> None:
        # transcript 用 JSONL 追加，最近镜像单独覆盖，便于后续 UI 快速读取。
        worker.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        with worker.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

        recent_events: list[dict[str, object]] = []
        if worker.recent_transcript_path.exists():
            recent_events = json.loads(worker.recent_transcript_path.read_text(encoding="utf-8"))
        recent_events.append(event)
        worker.recent_transcript_path.write_text(
            json.dumps(recent_events[-20:], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _create_request_record(
        self,
        *,
        request_type: str,
        from_name: str,
        to_name: str,
        summary: str,
    ) -> dict[str, object]:
        # request tracker 只记录最小配对信息，不重复保存整条消息正文。
        request_id = f"req-{uuid4().hex[:12]}"
        record = {
            "request_id": request_id,
            "from": from_name,
            "to": to_name,
            "type": request_type,
            "summary": summary,
            "status": "pending",
            "created_at": _utc_now(),
            "resolved_at": None,
        }
        with self._lock:
            self._request_tracker[request_id] = record
            self._save_request_tracker(self._request_tracker)
        return dict(record)

    def get_request_record(self, *, request_id: str) -> dict[str, object]:
        record = self._request_tracker.get(request_id)
        if record is None:
            raise ToolFailure(
                code="NOT_FOUND",
                message=f"未找到 request_id: {request_id}",
                text=f"未找到 request_id '{request_id}'。",
            )
        return dict(record)

    def _resolve_request_record(
        self,
        *,
        request_id: str,
        status: str,
    ) -> dict[str, object]:
        if status not in {"approved", "rejected"}:
            raise ToolFailure(
                code="INVALID_PARAM",
                message=f"非法请求状态: {status}",
                text="请求状态必须是 approved 或 rejected。",
            )
        with self._lock:
            record = self._request_tracker.get(request_id)
            if record is None:
                raise ToolFailure(
                    code="NOT_FOUND",
                    message=f"未找到 request_id: {request_id}",
                    text=f"未找到 request_id '{request_id}'。",
                )
            record["status"] = status
            record["resolved_at"] = _utc_now()
            self._save_request_tracker(self._request_tracker)
            return dict(record)

    def _build_worker_context(self, *, name: str, agent_id: str, sdk_session: SQLiteSession) -> ToolRuntimeContext:
        return ToolRuntimeContext(
            session_id=self.base_context.session_id,
            session_name=self.base_context.session_name,
            session=sdk_session,
            session_root=self.base_context.session_root,
            session_dir=self.base_context.session_dir,
            tasks_dir=self.base_context.tasks_dir,
            traces_dir=self.base_context.traces_dir,
            compaction_dir=self.base_context.compaction_dir,
            workspace_root=self.base_context.workspace_root,
            execution_root=self.base_context.execution_root,
            team_dir=self.base_context.team_dir,
            current_model=self.base_context.current_model,
            main_model=self.base_context.main_model,
            light_model=self.base_context.light_model,
            actor_name=name,
            team_runtime=self,
            trace_logger=build_trace_logger(
                f"{self.base_context.session_id}-{agent_id}",
                trace_dir=self.base_context.traces_dir,
            ),
        )

    def _claim_task_for_worker(self, worker: TeammateWorker) -> dict[str, object] | None:
        # teammate 空闲时，先尝试从 task board 里拿一条未被阻塞的任务。
        claimed_task = claim_persistent_task(
            tasks_dir=self.base_context.tasks_dir,
            owner_agent_id=worker.agent_id,
            owner=f"teammate:{worker.name}",
            lease_seconds=TEAM_TASK_LEASE_SECONDS,
        )
        if claimed_task is None:
            return None

        self._update_member(
            worker.name,
            status="working",
            current_task_id=claimed_task["id"],
        )
        self._append_transcript_event(
            worker,
            _build_transcript_event(
                event_type="task_claimed",
                payload={
                    "task_id": claimed_task["id"],
                    "title": claimed_task["title"],
                },
            ),
        )
        return claimed_task

    def _renew_task_lease_for_worker(self, worker: TeammateWorker, *, task_id: int) -> dict[str, object]:
        # 当前 teammate 只给自己持有的任务续租，避免别的 worker 篡改执行权。
        task = renew_task_lease(
            tasks_dir=self.base_context.tasks_dir,
            task_id=task_id,
            owner_agent_id=worker.agent_id,
            lease_seconds=TEAM_TASK_LEASE_SECONDS,
        )
        self._update_member(
            worker.name,
            current_task_id=task_id,
        )
        return task

    def _bind_task_execution_root_for_worker(
        self,
        worker: TeammateWorker,
        *,
        task: dict[str, object],
    ) -> dict[str, object]:
        # phase 4 只做最小 worktree 绑定：
        # 需要隔离的任务切到 task 专属 worktree，否则回到 session 的 workspace_root。
        if task.get("require_worktree"):
            task = ensure_task_worktree(
                runtime_context=self.base_context,
                task_id=int(task["id"]),
            )
            worktree_path = Path(str(task["worktree_path"]))
            worker.context.set_execution_root(worktree_path)
            self._update_member(
                worker.name,
                current_worktree=str(task["worktree_name"]),
            )
            self._append_transcript_event(
                worker,
                _build_transcript_event(
                    event_type="worktree_bound",
                    payload={
                        "task_id": task["id"],
                        "worktree_name": task["worktree_name"],
                    },
                ),
            )
            return task

        worker.context.set_execution_root(worker.context.workspace_root)
        self._update_member(
            worker.name,
            current_worktree=None,
        )
        return task

    def _finish_claimed_task(
        self,
        worker: TeammateWorker,
        *,
        task_id: int,
        final_output: str,
    ) -> dict[str, object]:
        # 如果 teammate 跑完这一轮后任务还停留在自己的 running lease 下，
        # 就把这轮结论直接写回任务图，作为最小 phase 3 闭环。
        task = get_task(self.base_context.tasks_dir, task_id)
        if task.get("status") == "running" and task.get("owner_agent_id") == worker.agent_id:
            task = update_task(
                tasks_dir=self.base_context.tasks_dir,
                task_id=task_id,
                status="completed",
                owner=f"teammate:{worker.name}",
                result_summary=final_output,
                error=None,
            )
        self._update_member(
            worker.name,
            status="idle",
            current_task_id=None,
        )
        self._append_transcript_event(
            worker,
            _build_transcript_event(
                event_type="task_finished",
                payload={
                    "task_id": task_id,
                    "status": task.get("status"),
                },
            ),
        )
        return task

    def _run_worker_loop(self, worker: TeammateWorker) -> None:
        # teammate 的生命周期是：创建后先进入 working，再立即转成 idle 等待消息。
        # 这能保证它"活着"，但不会在无任务时空转占用太多资源。
        self._update_member(worker.name, status="working")
        self._append_transcript_event(
            worker,
            _build_transcript_event(
                event_type="lifecycle",
                payload={"status": "working", "name": worker.name},
            ),
        )

        # 将 identity 信息写入独立 session，作为 teammate 的持久自我认知。
        # 恢复场景下 session 已有历史，不再重复注入。
        existing_items = asyncio.run(worker.sdk_session.get_items(limit=1))
        if not existing_items:
            identity_input = _build_teammate_identity_input(
                team_id=str(self._state["team_id"]),
                worker=worker,
                current_task_id=None,
            )
            asyncio.run(worker.sdk_session.add_items([identity_input]))

        self._update_member(worker.name, status="idle")

        while not worker.stop_event.is_set():
            claimed_task: dict[str, object] | None = None
            try:
                message = worker.message_queue.get(timeout=0.2)
            except queue.Empty:
                claimed_task = self._claim_task_for_worker(worker)
                if claimed_task is None:
                    continue
                message = None

            # shutdown 请求在 Phase 2 里先走最小自动批准路径：
            # 先回 shutdown_response，再让线程进入 stopping/stopped。
            if message is not None and message["type"] == "shutdown_request":
                self._append_transcript_event(
                    worker,
                    _build_transcript_event(
                        event_type="message",
                        payload=message,
                    ),
                )
                request_id = str(message.get("request_id") or "")
                if request_id:
                    self.respond_shutdown_request(
                        actor_name=worker.name,
                        request_id=request_id,
                        status="approved",
                        feedback="teammate 已接受关闭请求。",
                    )
                break

            # 普通消息会先写 transcript，再交给 teammate 跑一轮。
            self._update_member(worker.name, status="working")
            run_span_id = _generate_span_id()
            if claimed_task is None:
                self._append_transcript_event(
                    worker,
                    _build_transcript_event(
                        event_type="message",
                        payload=message,
                        span_id=run_span_id,
                    ),
                )
                current_input = _build_message_input(message)
            else:
                self._renew_task_lease_for_worker(worker, task_id=int(claimed_task["id"]))
                claimed_task = self._bind_task_execution_root_for_worker(
                    worker,
                    task=claimed_task,
                )
                current_input = _build_task_input(claimed_task)

            agent = Agent(
                name=worker.name,
                instructions=_build_teammate_instructions(
                    name=worker.name,
                    role=worker.role,
                ),
                model=worker.context.main_model or worker.context.current_model or "gpt-5.2-codex",
                tools=_build_teammate_tools(),
            )

            run_start_ms = int(datetime.now().timestamp() * 1000)
            try:
                # 每个 teammate 拥有独立 SQLiteSession，由 SDK 管理对话历史和持久化。
                result = asyncio.run(
                    Runner.run(
                        agent,
                        input=[current_input],
                        session=worker.sdk_session,
                        context=worker.context,
                        max_turns=30,
                    )
                )
            except Exception as exc:
                run_duration_ms = int(datetime.now().timestamp() * 1000) - run_start_ms
                # 这里只把 teammate 标成 failed，不在这里打复杂恢复补丁。
                self._append_transcript_event(
                    worker,
                    _build_transcript_event(
                        event_type="error",
                        payload={"message": str(exc)},
                        span_id=_generate_span_id(),
                        parent_span_id=run_span_id,
                        duration_ms=run_duration_ms,
                    ),
                )
                if claimed_task is not None:
                    update_task(
                        tasks_dir=self.base_context.tasks_dir,
                        task_id=int(claimed_task["id"]),
                        status="failed",
                        owner=f"teammate:{worker.name}",
                        error=str(exc),
                    )
                    self._update_member(worker.name, current_task_id=None)
                self._update_member(worker.name, status="failed")
                return

            run_duration_ms = int(datetime.now().timestamp() * 1000) - run_start_ms
            final_output = result.final_output if isinstance(result.final_output, str) else str(result.final_output or "")
            self._append_transcript_event(
                worker,
                _build_transcript_event(
                    event_type="assistant",
                    payload={"content": final_output},
                    span_id=run_span_id,
                    duration_ms=run_duration_ms,
                ),
            )
            if not final_output.strip():
                self.send_message(
                    from_name=worker.name,
                    to_name="team-lead",
                    content="(teammate 本轮未产生可读输出)",
                    summary="空回复通知",
                    message_type="message",
                )
            if claimed_task is None:
                self._update_member(worker.name, status="idle")
            else:
                self._finish_claimed_task(
                    worker,
                    task_id=int(claimed_task["id"]),
                    final_output=final_output,
                )

        # 收到停止请求后，线程最终会收敛到 stopped。
        self._update_member(worker.name, status="stopped")
        self._append_transcript_event(
            worker,
            _build_transcript_event(
                event_type="lifecycle",
                payload={"status": "stopped", "name": worker.name},
            ),
        )

    def spawn_teammate(self, *, name: str, role: str, prompt: str, persistent: bool = False) -> dict[str, object]:
        with self._lock:
            existing_member = self._find_member(name)
            existing_status = str(existing_member.get("status") or "") if existing_member is not None else ""
            if existing_member is not None and existing_status not in {"stopped", "failed"}:
                raise ToolFailure(
                    code="ALREADY_EXISTS",
                    message=f"teammate '{name}' 已存在。",
                    text=f"无法创建 teammate：'{name}' 已存在。",
                )

            # stopped/failed teammate 允许原位重建。
            # 这样 lead 不需要为了同一个角色反复发明新名字。
            agent_id = f"teammate-{uuid4().hex[:10]}"
            transcript_path = self.transcripts_dir / f"{agent_id}.jsonl"
            recent_transcript_path = self.transcripts_dir / f"{agent_id}_recent.json"
            member = {
                "agent_id": agent_id,
                "name": name,
                "role": role,
                "prompt": prompt,
                "status": "spawning",
                "current_task_id": None,
                "current_worktree": None,
                "persistent": persistent,
                "transcript_path": str(transcript_path.relative_to(self.base_context.session_dir)),
            }
            if existing_member is None:
                self._state["members"].append(member)
            else:
                existing_member.clear()
                existing_member.update(member)
            self._state["updated_at"] = _utc_now()
            self._save_state(self._state)

        # 真正的 worker 线程在状态落盘之后再启动，避免 UI 先看到线程活着但 state 还没写。
        # 如果这是一次重建，新的 worker 会覆盖旧的进程内句柄。
        # 每个 teammate 拥有独立 SQLiteSession，历史由 SDK 自动管理和持久化。
        teammate_session = SQLiteSession(
            session_id=agent_id,
            db_path=self.sessions_dir / f"{agent_id}.db",
        )
        worker = TeammateWorker(
            agent_id=agent_id,
            name=name,
            role=role,
            prompt=prompt,
            transcript_path=transcript_path,
            recent_transcript_path=recent_transcript_path,
            context=self._build_worker_context(name=name, agent_id=agent_id, sdk_session=teammate_session),
            sdk_session=teammate_session,
            message_queue=queue.Queue(),
        )
        thread = threading.Thread(
            target=self._run_worker_loop,
            args=(worker,),
            daemon=True,
            name=f"teammate-{name}",
        )
        worker.thread = thread
        self._workers[name] = worker
        thread.start()
        self.send_message(
            from_name="team-lead",
            to_name=name,
            content=prompt,
            summary="初始任务",
            message_type="message",
        )
        return {
            "team_id": self._state["team_id"],
            "member": dict(member),
        }

    def list_teammates(self) -> dict[str, object]:
        # 列表接口只返回持久化 member 视图，不暴露线程对象等进程内细节。
        return {
            "team_id": self._state["team_id"],
            "team_name": self._state["team_name"],
            "members": [dict(member) for member in self._state["members"]],
        }

    def claim_next_task(self, *, actor_name: str) -> dict[str, object]:
        # ClaimTask 只给 teammate 用，lead 不直接参与任务认领。
        worker = self._workers.get(actor_name)
        member = self._find_member(actor_name)
        if worker is None or member is None:
            raise ToolFailure(
                code="NOT_FOUND",
                message=f"未找到 teammate: {actor_name}",
                text=f"未找到 teammate '{actor_name}'。",
            )
        current_task_id = member.get("current_task_id")
        if current_task_id is not None:
            return {
                "claimed": False,
                "task": get_task(self.base_context.tasks_dir, int(current_task_id)),
            }

        claimed_task = self._claim_task_for_worker(worker)
        return {
            "claimed": claimed_task is not None,
            "task": claimed_task,
        }

    def send_message(
        self,
        *,
        from_name: str,
        to_name: str,
        content: str,
        summary: str | None,
        message_type: str,
        request_id: str | None = None,
        request_status: str | None = None,
    ) -> dict[str, object]:
        # 所有团队消息先统一组装成一份标准信封，再按目标路由到 lead 或 teammate。
        message = {
            "message_id": f"msg-{uuid4().hex[:12]}",
            "team_id": self._state["team_id"],
            "from": from_name,
            "to": to_name,
            "type": message_type,
            "summary": (summary or "").strip(),
            "content": content,
            "request_id": request_id,
            "request_status": request_status,
            "created_at": _utc_now(),
        }

        if to_name == "team-lead":
            self._lead_queue.put(message)
            return message

        # 发送给 teammate 时，要确保对方还处于可接收消息的状态。
        worker = self._workers.get(to_name)
        member = self._find_member(to_name)
        member_status = str(member.get("status") or "") if member is not None else ""
        if worker is None or member is None or member_status in {"stopped", "failed", "stopping"}:
            raise ToolFailure(
                code="NOT_FOUND",
                message=f"未找到可用 teammate: {to_name}",
                text=f"未找到可用 teammate '{to_name}'。",
            )
        worker.message_queue.put(message)
        return message

    def request_shutdown(
        self,
        *,
        from_name: str,
        teammate_name: str,
        content: str,
    ) -> dict[str, object]:
        if from_name != "team-lead":
            raise ToolFailure(
                code="ACCESS_DENIED",
                message="只有 team-lead 可以发起 shutdown_request。",
                text="访问被拒绝：只有 team-lead 可以发起关闭请求。",
            )
        record = self._create_request_record(
            request_type="shutdown_request",
            from_name=from_name,
            to_name=teammate_name,
            summary="停止 teammate",
        )
        message = self.send_message(
            from_name=from_name,
            to_name=teammate_name,
            content=content,
            summary="停止 teammate",
            message_type="shutdown_request",
            request_id=str(record["request_id"]),
            request_status="pending",
        )
        return {
            "request_id": record["request_id"],
            "message": message,
            "status": record["status"],
        }

    def respond_shutdown_request(
        self,
        *,
        actor_name: str,
        request_id: str,
        status: str,
        feedback: str | None = None,
    ) -> dict[str, object]:
        # shutdown_response 必须由被请求关闭的 teammate 自己返回。
        original_record = self.get_request_record(request_id=request_id)
        if actor_name != str(original_record["to"]):
            raise ToolFailure(
                code="ACCESS_DENIED",
                message="shutdown_response 的响应者不匹配。",
                text="访问被拒绝：只有被请求关闭的 teammate 才能响应这个 shutdown request。",
            )
        record = self._resolve_request_record(
            request_id=request_id,
            status=status,
        )
        if status == "approved":
            self._update_member(actor_name, status="stopping")
            worker = self._workers.get(actor_name)
            if worker is not None:
                worker.stop_event.set()
        message = self.send_message(
            from_name=actor_name,
            to_name=str(record["from"]),
            content=(feedback or "").strip(),
            summary="shutdown response",
            message_type="shutdown_response",
            request_id=request_id,
            request_status=status,
        )
        return {
            "request": record,
            "message": message,
        }

    def request_plan_review(
        self,
        *,
        actor_name: str,
        summary: str,
        content: str,
        to_name: str = "team-lead",
    ) -> dict[str, object]:
        record = self._create_request_record(
            request_type="plan_review_request",
            from_name=actor_name,
            to_name=to_name,
            summary=summary,
        )
        message = self.send_message(
            from_name=actor_name,
            to_name=to_name,
            content=content,
            summary=summary,
            message_type="plan_review_request",
            request_id=str(record["request_id"]),
            request_status="pending",
        )
        return {
            "request_id": record["request_id"],
            "message": message,
            "status": record["status"],
        }

    def respond_plan_review(
        self,
        *,
        actor_name: str,
        request_id: str,
        status: str,
        feedback: str | None = None,
    ) -> dict[str, object]:
        # plan_review_response 只能由原本被请求审阅的一方给出。
        original_record = self.get_request_record(request_id=request_id)
        if actor_name != str(original_record["to"]):
            raise ToolFailure(
                code="ACCESS_DENIED",
                message="plan_review_response 的响应者不匹配。",
                text="访问被拒绝：只有被请求审阅的一方才能响应这个计划审阅请求。",
            )
        record = self._resolve_request_record(
            request_id=request_id,
            status=status,
        )
        message = self.send_message(
            from_name=actor_name,
            to_name=str(record["from"]),
            content=(feedback or "").strip(),
            summary="plan review response",
            message_type="plan_review_response",
            request_id=request_id,
            request_status=status,
        )
        return {
            "request": record,
            "message": message,
        }

    def drain_lead_messages(self) -> list[dict[str, object]]:
        # lead 队列只在每轮 build_context 前排空一次，避免重复注入同一批消息。
        messages: list[dict[str, object]] = []
        while True:
            try:
                messages.append(self._lead_queue.get_nowait())
            except queue.Empty:
                break
        return messages

    def drain_teammate_state_changes(self) -> list[dict[str, object]]:
        # teammate 状态变化只给 lead 的 UI/runtime 看，不回注入模型正文。
        changes: list[dict[str, object]] = []
        while True:
            try:
                changes.append(self._lead_state_queue.get_nowait())
            except queue.Empty:
                break
        return changes

    def stop_teammate(self, *, name: str) -> dict[str, object]:
        # stop 只是发出关闭请求，不阻塞等待完整清理结束。
        worker = self._workers.get(name)
        member = self._find_member(name)
        if worker is None or member is None:
            raise ToolFailure(
                code="NOT_FOUND",
                message=f"未找到 teammate: {name}",
                text=f"未找到 teammate '{name}'。",
            )

        worker.stop_event.set()
        worker.message_queue.put(
            {
                "message_id": f"msg-{uuid4().hex[:12]}",
                "team_id": self._state["team_id"],
                "from": "team-lead",
                "to": name,
                "type": "shutdown_request",
                "summary": "停止 teammate",
                "content": "请结束当前 teammate 运行。",
                "request_id": None,
                "created_at": _utc_now(),
            }
        )
        return {"name": name, "status": "stopping"}

    def clear_worktree_binding(self, *, worktree_path: str) -> None:
        # closeout remove 后，把仍指向这个 worktree 的 teammate 状态收回到 workspace_root。
        target_worktree_path = str(Path(worktree_path).resolve())
        for worker in self._workers.values():
            member = self._find_member(worker.name)
            if member is None:
                continue
            if str(member.get("current_worktree") or "") == "":
                continue
            current_execution_root = str(worker.context.execution_root)
            if current_execution_root != target_worktree_path:
                continue
            worker.context.set_execution_root(worker.context.workspace_root)
            self._update_member(worker.name, current_worktree=None)

    def close(self) -> None:
        # 关闭 session runtime 时，把当前进程里的 teammate 一并停止，避免泄漏后台线程。
        for name in list(self._workers.keys()):
            try:
                self.stop_teammate(name=name)
            except ToolFailure:
                continue
        for worker in list(self._workers.values()):
            if worker.thread is not None:
                worker.thread.join(timeout=1)
            worker.sdk_session.close()


def build_agent_team_runtime(
    *,
    runtime_context: ToolRuntimeContext,
    team_name: str | None = None,
) -> AgentTeamRuntime:
    # team runtime 是 session 级对象，所以这里直接挂在 runtime_context 上复用。
    runtime = AgentTeamRuntime(
        session_id=runtime_context.session_id,
        session_name=runtime_context.session_name,
        team_dir=runtime_context.team_dir,
        base_context=runtime_context,
        team_name=team_name,
    )
    # 恢复上次会话中未完成的 teammate（有未完成 task 或标记为 persistent 的）。
    runtime._recover_teammates()
    return runtime


def spawn_teammate(
    runtime_context: ToolRuntimeContext,
    *,
    name: str,
    role: str,
    prompt: str,
    persistent: bool = False,
) -> dict[str, object]:
    # 这些顶层 helper 只做一层薄转发，让 team_tools 和测试不用直接碰 runtime 内部字段。
    if runtime_context.team_runtime is None:
        raise ToolFailure(
            code="NO_TEAM_RUNTIME",
            message="当前没有 team runtime。",
            text="当前 session 还没有可用的 team runtime。",
        )
    return runtime_context.team_runtime.spawn_teammate(
        name=name, role=role, prompt=prompt, persistent=persistent,
    )


def list_teammates(runtime_context: ToolRuntimeContext) -> dict[str, object]:
    if runtime_context.team_runtime is None:
        raise ToolFailure(
            code="NO_TEAM_RUNTIME",
            message="当前没有 team runtime。",
            text="当前 session 还没有可用的 team runtime。",
        )
    return runtime_context.team_runtime.list_teammates()


def send_team_message(
    runtime_context: ToolRuntimeContext,
    *,
    to: str,
    content: str,
    summary: str | None = None,
    message_type: str = "message",
) -> dict[str, object]:
    # 发送者身份来自当前 context.actor_name。
    # 这样 team-lead 和 teammate 都能复用同一条发送路径。
    if runtime_context.team_runtime is None:
        raise ToolFailure(
            code="NO_TEAM_RUNTIME",
            message="当前没有 team runtime。",
            text="当前 session 还没有可用的 team runtime。",
        )
    return runtime_context.team_runtime.send_message(
        from_name=runtime_context.actor_name,
        to_name=to,
        content=content,
        summary=summary,
        message_type=message_type,
    )


def request_shutdown(
    runtime_context: ToolRuntimeContext,
    *,
    name: str,
    content: str,
) -> dict[str, object]:
    if runtime_context.team_runtime is None:
        raise ToolFailure(
            code="NO_TEAM_RUNTIME",
            message="当前没有 team runtime。",
            text="当前 session 还没有可用的 team runtime。",
        )
    return runtime_context.team_runtime.request_shutdown(
        from_name=runtime_context.actor_name,
        teammate_name=name,
        content=content,
    )


def claim_next_task(runtime_context: ToolRuntimeContext) -> dict[str, object]:
    if runtime_context.team_runtime is None:
        raise ToolFailure(
            code="NO_TEAM_RUNTIME",
            message="当前没有 team runtime。",
            text="当前 session 还没有可用的 team runtime。",
        )
    return runtime_context.team_runtime.claim_next_task(
        actor_name=runtime_context.actor_name,
    )


def respond_shutdown_request(
    runtime_context: ToolRuntimeContext,
    *,
    request_id: str,
    status: str,
    feedback: str | None = None,
) -> dict[str, object]:
    if runtime_context.team_runtime is None:
        raise ToolFailure(
            code="NO_TEAM_RUNTIME",
            message="当前没有 team runtime。",
            text="当前 session 还没有可用的 team runtime。",
        )
    return runtime_context.team_runtime.respond_shutdown_request(
        actor_name=runtime_context.actor_name,
        request_id=request_id,
        status=status,
        feedback=feedback,
    )


def request_plan_review(
    runtime_context: ToolRuntimeContext,
    *,
    summary: str,
    content: str,
    to: str = "team-lead",
) -> dict[str, object]:
    if runtime_context.team_runtime is None:
        raise ToolFailure(
            code="NO_TEAM_RUNTIME",
            message="当前没有 team runtime。",
            text="当前 session 还没有可用的 team runtime。",
        )
    return runtime_context.team_runtime.request_plan_review(
        actor_name=runtime_context.actor_name,
        summary=summary,
        content=content,
        to_name=to,
    )


def respond_plan_review(
    runtime_context: ToolRuntimeContext,
    *,
    request_id: str,
    status: str,
    feedback: str | None = None,
) -> dict[str, object]:
    if runtime_context.team_runtime is None:
        raise ToolFailure(
            code="NO_TEAM_RUNTIME",
            message="当前没有 team runtime。",
            text="当前 session 还没有可用的 team runtime。",
        )
    return runtime_context.team_runtime.respond_plan_review(
        actor_name=runtime_context.actor_name,
        request_id=request_id,
        status=status,
        feedback=feedback,
    )


def get_request_record(runtime_context: ToolRuntimeContext, *, request_id: str) -> dict[str, object]:
    if runtime_context.team_runtime is None:
        raise ToolFailure(
            code="NO_TEAM_RUNTIME",
            message="当前没有 team runtime。",
            text="当前 session 还没有可用的 team runtime。",
        )
    return runtime_context.team_runtime.get_request_record(request_id=request_id)


def stop_teammate(runtime_context: ToolRuntimeContext, *, name: str) -> dict[str, object]:
    if runtime_context.team_runtime is None:
        raise ToolFailure(
            code="NO_TEAM_RUNTIME",
            message="当前没有 team runtime。",
            text="当前 session 还没有可用的 team runtime。",
        )
    return runtime_context.team_runtime.request_shutdown(
        from_name=runtime_context.actor_name,
        teammate_name=name,
        content="请结束当前 teammate 运行。",
    )
