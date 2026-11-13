"""Small single-instance queue. Job payloads live in private per-job directories."""
import json
import sqlite3
import time
from pathlib import Path


def write_json(path, data):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding='utf-8')
    temp.replace(path)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / 'jobs.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)')

    def connect(self):
        db = sqlite3.connect(self.db, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def create(self, job_id, owner, title):
        with self.connect() as db:
            now = time.time()
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)', (job_id, owner, title, 'queued', now, now))

    def get(self, job_id, owner=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or (owner is not None and row['owner'] != owner):
            return None
        return dict(row)

    def update(self, job_id, state):
        with self.connect() as db:
            db.execute('UPDATE jobs SET state=?,updated=? WHERE id=?', (state, time.time(), job_id))

    def list(self, owner):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM jobs WHERE owner=? ORDER BY created DESC LIMIT 100', (owner,))]

    def next(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY updated LIMIT 1").fetchone()
        return dict(row) if row else None

    def pending(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='interrupted',updated=? WHERE state='running'", (time.time(),))

    def delete(self, job_id):
        with self.connect() as db:
            db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
