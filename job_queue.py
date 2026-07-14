"""
SQLite-backed render job queue.
Survives server restarts; works across cloud server / Mac worker separation.
"""
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from project_manager import APPDATA_DIR

DB_PATH = APPDATA_DIR / "jobs.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _init():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id             TEXT PRIMARY KEY,
                project_safe   TEXT NOT NULL,
                output_dir     TEXT DEFAULT '',
                batch_name     TEXT DEFAULT '',
                status         TEXT DEFAULT 'pending',
                progress_done  INTEGER DEFAULT 0,
                progress_total INTEGER DEFAULT 0,
                progress_label TEXT DEFAULT '',
                videos_ok      INTEGER DEFAULT 0,
                created_at     TEXT,
                updated_at     TEXT
            )
        """)
        c.execute("ALTER TABLE jobs ADD COLUMN videos_ok INTEGER DEFAULT 0"
                  ) if "videos_ok" not in [
            r[1] for r in c.execute("PRAGMA table_info(jobs)")
        ] else None
        c.execute("""
            CREATE TABLE IF NOT EXISTS job_logs (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id  TEXT NOT NULL,
                msg     TEXT,
                ts      TEXT
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_logs_job ON job_logs(job_id)"
        )


_init()


# ── Write ─────────────────────────────────────────────────────────────────────

def create_job(project_safe: str, output_dir: str = "", batch_name: str = "") -> str:
    jid = uuid.uuid4().hex[:10]
    now = _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO jobs (id, project_safe, output_dir, batch_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (jid, project_safe, output_dir, batch_name, now, now),
        )
    return jid


def claim(job_id: str) -> bool:
    with _conn() as c:
        r = c.execute(
            "UPDATE jobs SET status='running', updated_at=? WHERE id=? AND status='pending'",
            (_now(), job_id),
        )
        return r.rowcount > 0


def update_progress(job_id: str, done: int, total: int, label: str = ""):
    with _conn() as c:
        c.execute(
            "UPDATE jobs SET progress_done=?, progress_total=?, progress_label=?, updated_at=? "
            "WHERE id=?",
            (done, total, label, _now(), job_id),
        )


def add_log(job_id: str, msg: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO job_logs (job_id, msg, ts) VALUES (?, ?, ?)",
            (job_id, msg, _now()),
        )


def finish(job_id: str, status: str = "done", videos_ok: int = 0):
    with _conn() as c:
        c.execute(
            "UPDATE jobs SET status=?, videos_ok=?, updated_at=? WHERE id=?",
            (status, videos_ok, _now(), job_id),
        )


def cancel(job_id: str):
    with _conn() as c:
        # pending → cancelled ngay lập tức (worker chưa nhận)
        c.execute(
            "UPDATE jobs SET status='cancelled', updated_at=? "
            "WHERE id=? AND status='pending'",
            (_now(), job_id),
        )
        # running → cancelling (signal worker dừng)
        c.execute(
            "UPDATE jobs SET status='cancelling', updated_at=? "
            "WHERE id=? AND status='running'",
            (_now(), job_id),
        )


def cleanup_stale(timeout_seconds: int = 60):
    """Chuyển 'cancelling' quá lâu không update thành 'cancelled'."""
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(seconds=timeout_seconds)).isoformat(timespec="seconds")
    with _conn() as c:
        c.execute(
            "UPDATE jobs SET status='cancelled', updated_at=? "
            "WHERE status='cancelling' AND updated_at < ?",
            (_now(), cutoff),
        )


# ── Read ──────────────────────────────────────────────────────────────────────

def get_pending() -> list[dict]:
    with _conn() as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT * FROM jobs WHERE status='pending' ORDER BY created_at"
            )
        ]


def get_job(job_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(r) if r else None


def list_jobs(project_safe: str | None = None, limit: int = 50) -> list[dict]:
    with _conn() as c:
        if project_safe:
            return [dict(r) for r in c.execute(
                "SELECT * FROM jobs WHERE project_safe=? ORDER BY created_at DESC LIMIT ?",
                (project_safe, limit)
            )]
        return [dict(r) for r in c.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        )]


def latest_for_project(project_safe: str) -> dict | None:
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM jobs WHERE project_safe=? ORDER BY created_at DESC LIMIT 1",
            (project_safe,),
        ).fetchone()
        return dict(r) if r else None


def total_rendered_for_project(project_safe: str) -> int:
    with _conn() as c:
        r = c.execute(
            "SELECT COALESCE(SUM(videos_ok), 0) FROM jobs WHERE project_safe=? AND status='done'",
            (project_safe,),
        ).fetchone()
        return int(r[0]) if r else 0


def logs_since(job_id: str, since_id: int = 0) -> list[dict]:
    with _conn() as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT * FROM job_logs WHERE job_id=? AND id>? ORDER BY id",
                (job_id, since_id),
            )
        ]
