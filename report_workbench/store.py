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

    def create(self, job_id, workspace, title, mode, sources):
        timestamp = now()
        with self.connect() as conn:
            conn.execute("INSERT INTO jobs(id,workspace,title,mode,status,stage,sources,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (job_id, workspace, title, mode, "queued", "等待处理", json.dumps(sources, ensure_ascii=False), timestamp, timestamp))

    @staticmethod
    def decode(row):
        if row is None:
            return None
        item = dict(row)
        item.pop("workspace", None)
        item["sources"] = json.loads(item["sources"])
        if 'trace' in item:
            item['trace'] = json.loads(item['trace'])
        return item

    def get(self, job_id, workspace):
        with self.connect() as conn:
            return self.decode(conn.execute("SELECT * FROM jobs WHERE id=? AND workspace=?", (job_id, workspace)).fetchone())

    def history(self, workspace):
        with self.connect() as conn:
            rows = conn.execute("SELECT id,title,mode,status,stage,progress,sources,error,created_at,updated_at FROM jobs WHERE workspace=? ORDER BY created_at DESC LIMIT 100", (workspace,)).fetchall()
            return [self.decode(r) for r in rows]

    def update(self, job_id, **fields):
        allowed = {"status", "stage", "progress", "markdown", "error", "trace"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid update")
        fields["updated_at"] = now()
        with self.connect() as conn:
            conn.execute(f"UPDATE jobs SET {','.join(k + '=?' for k in fields)} WHERE id=? AND status NOT IN ('completed','failed','cancelled')", (*fields.values(), job_id))
