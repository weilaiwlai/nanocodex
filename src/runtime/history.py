"""SDK Session protocol with transactional replacement and revision fencing."""
from __future__ import annotations

import asyncio
from src.runtime.async_utils import async_call
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class HistoryConflict(RuntimeError):
    pass


class AtomicSQLiteSession:
    def __init__(self, session_id: str, db_path: str | Path):
        self.session_id = session_id
        self.session_settings = None
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS nano_sessions (id TEXT PRIMARY KEY, revision INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS nano_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, data TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS nano_messages_session ON nano_messages(session_id, id)')
            db.execute('BEGIN IMMEDIATE')
            exists = db.execute('SELECT 1 FROM nano_sessions WHERE id=?', (session_id,)).fetchone()
            if not exists:
                db.execute('INSERT INTO nano_sessions VALUES (?, 0)', (session_id,))
                # The SDK uses agent_messages(session_id, message_data, id).
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'agent_messages' in tables:
                    for row in db.execute('SELECT message_data FROM agent_messages WHERE session_id=? ORDER BY id', (session_id,)):
                        json.loads(row[0])  # Reject corrupt legacy history, preserving the original DB.
                        db.execute('INSERT INTO nano_messages(session_id,data) VALUES (?,?)', (session_id, row[0]))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            db.execute('PRAGMA busy_timeout=10000')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _snapshot(self, limit=None):
        with self._connect() as db:
            db.execute('BEGIN')
            revision = db.execute('SELECT revision FROM nano_sessions WHERE id=?', (self.session_id,)).fetchone()[0]
            rows = db.execute('SELECT data FROM nano_messages WHERE session_id=? ORDER BY id', (self.session_id,)).fetchall()
        items = [json.loads(row[0]) for row in rows]
        return (items if limit is None else items[-limit:] if limit > 0 else []), revision

    async def snapshot(self):
        return await async_call(self._snapshot)

    async def get_items(self, limit: int | None = None):
        return (await async_call(self._snapshot, limit))[0]

    def _change(self, action, items=(), expected_revision=None):
        serialized = [json.dumps(item, ensure_ascii=False) for item in items]
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            revision = db.execute('SELECT revision FROM nano_sessions WHERE id=?', (self.session_id,)).fetchone()[0]
            if expected_revision is not None and revision != expected_revision:
                raise HistoryConflict('历史已被其他运行更新，请重新构建上下文。')
            popped = None
            if action in {'replace', 'clear'}:
                db.execute('DELETE FROM nano_messages WHERE session_id=?', (self.session_id,))
            elif action == 'pop':
                row = db.execute('SELECT id,data FROM nano_messages WHERE session_id=? ORDER BY id DESC LIMIT 1', (self.session_id,)).fetchone()
                if row:
                    db.execute('DELETE FROM nano_messages WHERE id=?', (row[0],))
                    popped = json.loads(row[1])
            db.executemany('INSERT INTO nano_messages(session_id,data) VALUES (?,?)', [(self.session_id, value) for value in serialized])
            db.execute('UPDATE nano_sessions SET revision=revision+1 WHERE id=?', (self.session_id,))
        return popped

    async def add_items(self, items):
        if items:
            await async_call(self._change, 'add', items)

    async def replace_items(self, items, *, expected_revision: int):
        await async_call(self._change, 'replace', items, expected_revision)

    async def pop_item(self):
        return await async_call(self._change, 'pop')

    async def clear_session(self):
        await async_call(self._change, 'clear')

    def close(self):
        # Connections belong to individual transactions; no live connection to close.
        pass
