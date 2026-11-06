from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "reports.sqlite3"
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL, title TEXT NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0, sources TEXT NOT NULL,
                markdown TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_workspace ON jobs(workspace, created_at)")
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(jobs)')}
            if 'trace' not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN trace TEXT NOT NULL DEFAULT '[]'")
            for name, definition in {'template': "TEXT NOT NULL DEFAULT 'study'", 'batch_id': 'TEXT',
                                     'evidence': "TEXT NOT NULL DEFAULT '[]'"}.items():
                if name not in columns:
                    conn.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
            conn.execute('CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, title TEXT NOT NULL, created_at TEXT NOT NULL)')
            conn.execute("UPDATE jobs SET status='cancelled', stage='服务重启，任务已中止', updated_at=? WHERE status IN ('queued','running')", (now(),))

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, job_id, workspace, title, mode, sources, template='study', batch_id=None):
        timestamp = now()
        with self.connect() as conn:
            conn.execute("INSERT INTO jobs(id,workspace,title,mode,status,stage,sources,created_at,updated_at,template,batch_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (job_id, workspace, title, mode, "queued", "等待队列", json.dumps(sources, ensure_ascii=False), timestamp, timestamp, template, batch_id))

    def create_batch(self, batch_id, workspace, title, jobs):
        timestamp = now()
        with self.connect() as conn:
            conn.execute('INSERT INTO batches VALUES (?,?,?,?)', (batch_id, workspace, title, timestamp))
            for job_id, request in jobs:
                from .engine import metadata
                conn.execute("INSERT INTO jobs(id,workspace,title,mode,status,stage,sources,created_at,updated_at,template,batch_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                             (job_id, workspace, request.title, request.mode, 'queued', '等待队列', json.dumps([metadata(s) for s in request.sources], ensure_ascii=False), timestamp, timestamp, request.template, batch_id))

    def batch(self, batch_id, workspace):
        with self.connect() as conn:
            row = conn.execute('SELECT id,title,created_at FROM batches WHERE id=? AND workspace=?', (batch_id, workspace)).fetchone()
            if row is None:
                return None
            jobs = [self.decode(job) for job in conn.execute('SELECT * FROM jobs WHERE batch_id=? AND workspace=? ORDER BY rowid', (batch_id, workspace))]
        return {**dict(row), 'jobs': jobs, 'terminal': all(j['status'] in {'completed','failed','cancelled'} for j in jobs),
                'completed': sum(j['status'] == 'completed' for j in jobs)}

    def batches(self, workspace):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute('SELECT id,title,created_at FROM batches WHERE workspace=? ORDER BY created_at DESC LIMIT 50', (workspace,))]

    def pending(self, workspace):
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM jobs WHERE workspace=? AND status IN ('queued','running')", (workspace,)).fetchone()[0]

    @staticmethod
    def decode(row):
        if row is None:
            return None
        item = dict(row)
        item.pop("workspace", None)
        item["sources"] = json.loads(item["sources"])
        for key in ('trace', 'evidence'):
            if key in item:
                item[key] = json.loads(item[key])
        return item

    def get(self, job_id, workspace):
        with self.connect() as conn:
            return self.decode(conn.execute("SELECT * FROM jobs WHERE id=? AND workspace=?", (job_id, workspace)).fetchone())

    def history(self, workspace):
        with self.connect() as conn:
            rows = conn.execute("SELECT id,title,mode,status,stage,progress,sources,error,created_at,updated_at,template,batch_id FROM jobs WHERE workspace=? ORDER BY created_at DESC LIMIT 100", (workspace,)).fetchall()
            return [self.decode(r) for r in rows]

    def update(self, job_id, **fields):
        allowed = {"status", "stage", "progress", "markdown", "error", "trace", "evidence"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid update")
        fields["updated_at"] = now()
        with self.connect() as conn:
            conn.execute(f"UPDATE jobs SET {','.join(k + '=?' for k in fields)} WHERE id=? AND status NOT IN ('completed','failed','cancelled')", (*fields.values(), job_id))
