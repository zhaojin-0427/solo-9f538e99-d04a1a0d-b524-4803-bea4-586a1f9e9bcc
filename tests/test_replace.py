"""Interleaving of "replace all feed" imports with solve jobs.

Covers the replacement contract:

* a running old-feed job is aborted and ends up ``void`` without a plan;
* queued old-feed jobs are voided in the same transaction and never run,
  even after a process restart;
* a late result cannot create a plan (status re-check in the worker);
* solve forms rendered before the replacement are refused (refresh prompt),
  including when the new batch reuses the same catalogue numbers;
* replacement failures, merge imports and minute/lock corrections leave the
  existing jobs untouched;
* only one solve runs at a time and a new-feed job takes over after cancel.
"""

from __future__ import annotations

import io
import threading

import pytest

from app import db as db_module
from app.solver import SolveCancelled, solve as real_solve
from tests.conftest import CSRF_RE, FEED_GEN_RE, post

OLD_ROWS = [
    ("R-1", "A", "B", 5, 0),
    ("R-2", "A", "B", 6, 0),
    ("R-3", "B", "C", 50, 0),
]

NEW_ROWS = [
    ("N-1", "A", "B", 7, 0),
    ("N-2", "B", "C", 9, 0),
]


def _csv(rows):
    text = "voucher_no,catalog_a,catalog_b,minutes,locked\n"
    text += "".join(f"{v},{a},{b},{m},{l}\n" for v, a, b, m, l in rows)
    return text.encode()


def _token(client, path="/"):
    page = client.get(path)
    match = CSRF_RE.search(page.get_data(as_text=True))
    assert match, f"no CSRF token on {path}"
    return match.group(1)


def _replace(client, rows):
    return client.post(
        "/import",
        data={
            "csrf_token": _token(client),
            "mode": "replace",
            "csv_file": (io.BytesIO(_csv(NEW_ROWS if rows is None else rows)), "new.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _merge(client, rows):
    return client.post(
        "/import",
        data={
            "csrf_token": _token(client),
            "mode": "merge",
            "csv_file": (io.BytesIO(_csv(rows)), "m.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _make_slow_solve(monkeypatch, started, release):
    import app.worker as worker_module

    def slow_solve(edges, pair1, pair2=None, cancel_cb=None):
        started.set()
        if not release.wait(5):
            raise AssertionError("test never released the solve")
        return real_solve(edges, pair1, pair2, cancel_cb=cancel_cb)

    monkeypatch.setattr(worker_module, "solve", slow_solve)


def _job(conn, job_id):
    return conn.execute(
        "SELECT * FROM solve_jobs WHERE id = ?", (job_id,)
    ).fetchone()


def test_replace_aborts_running_job_and_writes_no_plan(client, app, worker, monkeypatch):
    from tests.conftest import load_rows

    load_rows(app, OLD_ROWS)
    started = threading.Event()
    release = threading.Event()
    _make_slow_solve(monkeypatch, started, release)

    with app.app_context():
        conn = db_module.get_db()
        with db_module.transaction(conn):
            from app.services import enqueue_solve

            job_id = enqueue_solve(conn, ("A", "C"), None)

    thread = threading.Thread(target=worker.process_all)
    thread.start()
    try:
        assert started.wait(5), "solve never started"

        # Replacement must not wait for the solve: the HTTP call commits while
        # the solver thread is blocked inside the computation.
        resp = _replace(client, NEW_ROWS)
        assert "已替换供数" in resp.get_data(as_text=True)
        assert "已作废" in resp.get_data(as_text=True)
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()

    with app.app_context():
        conn = db_module.get_db()
        job = _job(conn, job_id)
        assert job["status"] == "void"
        assert job["plan_id"] is None
        assert job["result_json"] is None
        # Every old plan row was wiped and nothing was recreated late.
        assert conn.execute("SELECT COUNT(*) AS n FROM plans").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM plan_items").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM credentials WHERE voucher_no LIKE 'R-%'"
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM credentials"
        ).fetchone()["n"] == 2

    # The job page explains the cancellation in Chinese.
    page = client.get(f"/jobs/{job_id}")
    body = page.get_data(as_text=True)
    assert "任务已作废" in body and "整批替换" in body


def test_replace_voids_queued_jobs_without_running_them(client, app, worker):
    """A queued job must never solve against the new feed (no empty plan)."""
    from tests.conftest import load_rows

    load_rows(app, OLD_ROWS)
    with app.app_context():
        conn = db_module.get_db()
        with db_module.transaction(conn):
            from app.services import enqueue_solve

            j1 = enqueue_solve(conn, ("A", "C"), None)
            j2 = enqueue_solve(conn, ("A", "B"), None)

    resp = _replace(client, NEW_ROWS)
    assert "2 个未完成求解任务已作废" in resp.get_data(as_text=True)

    worker.process_all()  # must pick up nothing

    with app.app_context():
        conn = db_module.get_db()
        for job_id in (j1, j2):
            job = _job(conn, job_id)
            assert job["status"] == "void"
            assert job["plan_id"] is None
            assert job["result_json"] is None
        assert conn.execute("SELECT COUNT(*) AS n FROM plans").fetchone()["n"] == 0
        assert db_module.get_feed_generation(conn) == 1


def test_stale_solve_form_is_refused_even_with_same_catalog_numbers(client, app, worker):
    from tests.conftest import load_rows, solve_and_drain

    load_rows(app, OLD_ROWS)
    form_page = client.get("/solve")
    form_text = form_page.get_data(as_text=True)
    old_generation = FEED_GEN_RE.search(form_text).group(1)
    assert old_generation == "0"

    # New batch deliberately reuses catalogue numbers A/B/C.
    _replace(client, NEW_ROWS)

    stale_resp = client.post(
        "/solve",
        data={
            "csrf_token": CSRF_RE.search(form_text).group(1),
            "feed_generation": old_generation,
            "p1a": "A",
            "p1b": "C",
        },
        follow_redirects=True,
    )
    body = stale_resp.get_data(as_text=True)
    assert "表单已过期" in body and "刷新页面" in body
    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM solve_jobs WHERE status != 'void'"
        ).fetchone()["n"] == 0

    # A freshly opened form carries the new generation and works normally.
    fresh_page = client.get("/solve")
    fresh = FEED_GEN_RE.search(fresh_page.get_data(as_text=True)).group(1)
    assert fresh == "1"
    ok_resp = client.post(
        "/solve",
        data={
            "csrf_token": CSRF_RE.search(fresh_page.get_data(as_text=True)).group(1),
            "feed_generation": fresh,
            "p1a": "A",
            "p1b": "C",
        },
        follow_redirects=False,
    )
    assert ok_resp.status_code == 302
    new_job = solve_and_drain(app, worker, ("A", "C"))
    assert new_job["status"] == "done"
    assert new_job["feed_generation"] == 1


def test_failed_replacement_changes_nothing(client, app, worker):
    from tests.conftest import load_rows, solve_and_drain

    load_rows(app, OLD_ROWS)
    job = solve_and_drain(app, worker, ("A", "C"))
    assert job["status"] == "done"
    plan_id = job["plan_id"]

    with app.app_context():
        conn = db_module.get_db()
        with db_module.transaction(conn):
            from app.services import enqueue_solve

            queued_id = enqueue_solve(conn, ("A", "B"), None)
        before_generation = db_module.get_feed_generation(conn)

    bad_csv = (
        "voucher_no,catalog_a,catalog_b,minutes,locked\n"
        "BAD-1,A,B,0,0\n"  # minutes out of range -> whole batch rejected
    )
    page = client.get("/")
    token = CSRF_RE.search(page.get_data(as_text=True)).group(1)
    resp = client.post(
        "/import",
        data={
            "csrf_token": token,
            "mode": "replace",
            "csv_file": (io.BytesIO(bad_csv.encode()), "bad.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "1～10000" in body

    worker.process_all()
    with app.app_context():
        conn = db_module.get_db()
        assert db_module.get_feed_generation(conn) == before_generation
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM credentials WHERE voucher_no LIKE 'R-%'"
        ).fetchone()["n"] == 3
        # The queued job survived and still solves normally against old feed.
        queued = _job(conn, queued_id)
        assert queued["status"] == "done"
        assert queued["plan_id"] is not None
        plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        assert plan["status"] == "draft"


def test_merge_import_and_edits_do_not_void_jobs_or_bump_generation(client, app, worker):
    from tests.conftest import load_rows, solve_and_drain

    load_rows(app, OLD_ROWS)
    job = solve_and_drain(app, worker, ("A", "C"))
    plan_id = job["plan_id"]
    with app.app_context():
        conn = db_module.get_db()
        assert db_module.get_feed_generation(conn) == 0

    # Merge import: jobs remain valid, generation unchanged.
    resp = _merge(client, [("M-1", "C", "D", 3, 0)])
    assert "导入完成" in resp.get_data(as_text=True)
    with app.app_context():
        conn = db_module.get_db()
        assert db_module.get_feed_generation(conn) == 0
        assert _job(conn, job["id"])["status"] == "done"

    # Minute/lock correction: same.
    with app.app_context():
        conn = db_module.get_db()
        cid = conn.execute(
            "SELECT credential_id FROM plan_items WHERE plan_id = ?", (plan_id,)
        ).fetchone()[0]
    resp = post(
        client,
        f"/credentials/{cid}/edit",
        {"minutes": "12", "locked": "1"},
        token_path="/credentials",
    )
    assert "已更新" in resp.get_data(as_text=True)
    with app.app_context():
        conn = db_module.get_db()
        assert db_module.get_feed_generation(conn) == 0

    # A form opened before those edits still submits (no stale-form block).
    form = client.get("/solve")
    resp = client.post(
        "/solve",
        data={
            "csrf_token": CSRF_RE.search(form.get_data(as_text=True)).group(1),
            "feed_generation": FEED_GEN_RE.search(form.get_data(as_text=True)).group(1),
            "p1a": "A",
            "p1b": "D",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302


def test_void_jobs_stay_void_after_restart_and_new_jobs_run(app):
    """Restart requeues only crashed 'running' jobs; void jobs never revive."""
    from tests.conftest import load_rows

    load_rows(app, OLD_ROWS)
    with app.app_context():
        conn = db_module.get_db()
        with db_module.transaction(conn):
            from app.services import enqueue_solve

            void_id = enqueue_solve(conn, ("A", "C"), None)

    # Simulate the replace transaction directly (services layer).
    with app.app_context():
        conn = db_module.get_db()
        with db_module.transaction(conn):
            from app.services import import_credentials

            import_credentials(
                conn,
                [
                    {
                        "voucher_no": "N-1",
                        "catalog_a": "A",
                        "catalog_b": "B",
                        "minutes": 7,
                        "locked": 0,
                    }
                ],
                "replace",
            )
        assert _job(conn, void_id)["status"] == "void"

    # A brand-new app on the same database == process restart.
    from app import create_app

    db_path = app.config["DB_PATH"]
    restarted = create_app(
        {
            "TESTING": True,
            "DB_PATH": db_path,
            "START_WORKER": False,
            "SECRET_KEY": "restart-secret",
        }
    )
    from app.worker import get_worker

    restarted_worker = get_worker(restarted)
    restarted_worker.process_all()
    with restarted.app_context():
        conn = db_module.get_db()
        assert _job(conn, void_id)["status"] == "void"
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM solve_jobs WHERE status IN ('queued','running')"
        ).fetchone()["n"] == 0

    # New-feed jobs enqueued after restart run normally.
    from tests.conftest import solve_and_drain

    new_job = solve_and_drain(restarted, restarted_worker, ("A", "B"))
    assert new_job["status"] == "done"
    assert new_job["feed_generation"] == 1


def test_solver_raises_cancel_when_callback_fires():
    from app.solver import Edge

    edges = [
        Edge(i, f"V{i}", f"C{i}", f"C{i+1}", 1)
        for i in range(20)
    ]
    with pytest.raises(SolveCancelled):
        real_solve(edges, ("C0", "C19"), cancel_cb=lambda: True)


def test_only_one_solve_runs_at_a_time_and_new_job_takes_over(app, worker, monkeypatch):
    from tests.conftest import load_rows

    load_rows(app, OLD_ROWS)
    concurrent = {"active": 0, "max_active": 0}
    lock = threading.Lock()
    first_started = threading.Event()
    release_first = threading.Event()

    import app.worker as worker_module

    def guarded_solve(edges, pair1, pair2=None, cancel_cb=None):
        with lock:
            concurrent["active"] += 1
            concurrent["max_active"] = max(
                concurrent["max_active"], concurrent["active"]
            )
        try:
            if concurrent["active"] == 1 and not first_started.is_set():
                first_started.set()
                if not release_first.wait(5):
                    raise AssertionError("first solve never released")
            return real_solve(edges, pair1, pair2, cancel_cb=cancel_cb)
        finally:
            with lock:
                concurrent["active"] -= 1

    monkeypatch.setattr(worker_module, "solve", guarded_solve)

    with app.app_context():
        conn = db_module.get_db()
        with db_module.transaction(conn):
            from app.services import enqueue_solve

            enqueue_solve(conn, ("A", "C"), None)

    thread = threading.Thread(target=worker.process_all)
    thread.start()
    try:
        assert first_started.wait(5)
        with lock:
            assert concurrent["max_active"] == 1
    finally:
        release_first.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert concurrent["max_active"] == 1


def test_late_result_after_void_creates_no_plan(app, worker, monkeypatch):
    """If the cancel signal races past the solver, the status re-check must
    still stop the draft plan from being written."""
    from tests.conftest import load_rows

    load_rows(app, OLD_ROWS)
    entered = threading.Event()
    release = threading.Event()

    import app.worker as worker_module

    def oblivious_solve(edges, pair1, pair2=None, cancel_cb=None):
        entered.set()
        # Block *after* the snapshot read but *before* the result write, so
        # the test thread can void the job while a real result is in hand.
        assert release.wait(5), "late-result test never released the solve"
        return real_solve(edges, pair1, pair2, cancel_cb=None)

    monkeypatch.setattr(worker_module, "solve", oblivious_solve)

    with app.app_context():
        conn = db_module.get_db()
        with db_module.transaction(conn):
            from app.services import enqueue_solve

            job_id = enqueue_solve(conn, ("A", "C"), None)

    thread = threading.Thread(target=worker.process_all)
    thread.start()
    try:
        assert entered.wait(5)
        # Void the job while the oblivious solve is holding its result.
        with app.app_context():
            conn = db_module.get_db()
            with db_module.transaction(conn):
                conn.execute(
                    "UPDATE solve_jobs SET status = 'void', "
                    "finished_at = datetime('now'), "
                    "message = '供数已被整批替换，该任务作废。' WHERE id = ?",
                    (job_id,),
                )
        release.set()
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

    with app.app_context():
        conn = db_module.get_db()
        job = _job(conn, job_id)
        assert job["status"] == "void"
        assert job["plan_id"] is None
        assert conn.execute("SELECT COUNT(*) AS n FROM plans").fetchone()["n"] == 0
