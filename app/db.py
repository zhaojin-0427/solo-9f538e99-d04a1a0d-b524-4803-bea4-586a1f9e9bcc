from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from flask import current_app, g

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection configured for this application.

    WAL lets background solves read a consistent snapshot while the cataloguer
    keeps editing review minutes or lock flags.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db() -> None:
    conn = get_db()
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('data_version', '0')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('feed_generation', '0')"
    )
    _migrate_solve_jobs(conn)


def _migrate_solve_jobs(conn: sqlite3.Connection) -> None:
    """Upgrade databases created before the ``void`` status / feed generation.

    The CHECK constraint cannot be altered in place, so the table is rebuilt
    (SQLite's documented table-rebuild pattern).  All existing rows are
    preserved and unfinished jobs keep their status; only queued/running jobs
    that predate a generation tracking are stamped with generation 0.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(solve_jobs)")}
    if "feed_generation" in cols:
        return
    conn.executescript(
        """
        ALTER TABLE solve_jobs RENAME TO solve_jobs_old;
        CREATE TABLE solve_jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            status          TEXT    NOT NULL DEFAULT 'queued'
                            CHECK (status IN ('queued', 'running', 'done', 'nosolution',
                                              'error', 'void')),
            pair1_a         TEXT    NOT NULL,
            pair1_b         TEXT    NOT NULL,
            pair2_a         TEXT,
            pair2_b         TEXT,
            data_version    INTEGER NOT NULL,
            feed_generation INTEGER NOT NULL DEFAULT 0,
            result_total    INTEGER,
            result_json     TEXT,
            message         TEXT,
            plan_id         INTEGER REFERENCES plans(id) ON DELETE SET NULL,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            started_at      TEXT,
            finished_at     TEXT
        );
        INSERT INTO solve_jobs
            (id, status, pair1_a, pair1_b, pair2_a, pair2_b, data_version,
             feed_generation, result_total, result_json, message, plan_id,
             created_at, started_at, finished_at)
        SELECT id, status, pair1_a, pair1_b, pair2_a, pair2_b, data_version,
               0, result_total, result_json, message, plan_id,
               created_at, started_at, finished_at
        FROM solve_jobs_old;
        DROP TABLE solve_jobs_old;
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON solve_jobs(status);
        """
    )


def get_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()
    return int(row["value"]) if row else 0


def bump_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
        "WHERE key = 'data_version'"
    )
    return get_version(conn)


def get_feed_generation(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'feed_generation'"
    ).fetchone()
    return int(row["value"]) if row else 0


def bump_feed_generation(conn: sqlite3.Connection) -> int:
    conn.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
        "WHERE key = 'feed_generation'"
    )
    return get_feed_generation(conn)


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Explicit immediate write transaction; always rolls back on failure."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


@contextmanager
def read_snapshot(conn: sqlite3.Connection):
    """Short consistent read snapshot, released before long computations."""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")
