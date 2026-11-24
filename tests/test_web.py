from __future__ import annotations

import io
import json
import sqlite3

import pytest

from app import create_app
from app import db as db_module
from app.services import parse_csv
from tests.conftest import csrf, load_rows, post, solve_and_drain

BASE_ROWS = [
    ("V-1", "A", "B", 10, 0),
    ("V-2", "A", "B", 6, 0),
    ("V-3", "B", "C", 5, 0),
    ("V-4", "C", "D", 7, 0),
    ("V-5", "B", "D", 3, 1),
]


def test_import_validation_is_atomic(client, app):
    load_rows(app, BASE_ROWS)
    bad_csv = (
        "voucher_no,catalog_a,catalog_b,minutes,locked\n"
        "V-9,A,B,0,0\n"
        "V-10,A,C,30,2\n"
    )
    response = post(
        client,
        "/import",
        {"mode": "merge"},
    )
    # no file -> friendly error, nothing changes
    assert response.status_code == 200
    response = client.post(
        "/import",
        data={
            "csrf_token": _token(client),
            "mode": "merge",
            "csv_file": (io.BytesIO(bad_csv.encode()), "bad.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert "第 2 行" in body and "第 3 行" in body
    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"] == 5


def _token(client):
    return csrf(client)


def test_import_merge_and_replace(client, app):
    csv_one = (
        "voucher_no,catalog_a,catalog_b,minutes,locked\n"
        "V-1,A,B,10,0\n"
        "V-2,A,B,6,0\n"
    )
    resp = client.post(
        "/import",
        data={
            "csrf_token": _token(client),
            "mode": "merge",
            "csv_file": (io.BytesIO(csv_one.encode()), "one.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "新增 2 份" in resp.get_data(as_text=True)

    csv_two = (
        "voucher_no,catalog_a,catalog_b,minutes,locked\n"
        "V-1,A,B,12,1\n"
        "V-3,B,C,5,0\n"
    )
    resp = client.post(
        "/import",
        data={
            "csrf_token": _token(client),
            "mode": "merge",
            "csv_file": (io.BytesIO(csv_two.encode()), "two.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "新增 1 份" in body and "更新 1 份" in body
    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT minutes, locked FROM credentials WHERE voucher_no = 'V-1'"
        ).fetchone()
        assert (row["minutes"], row["locked"]) == (12, 1)

    resp = client.post(
        "/import",
        data={
            "csrf_token": _token(client),
            "mode": "replace",
            "csv_file": (io.BytesIO(csv_one.encode()), "one.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "已替换供数" in resp.get_data(as_text=True)
    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"] == 2


def test_parse_csv_rejects_duplicates_and_bad_ranges():
    payload = (
        "voucher_no,catalog_a,catalog_b,minutes,locked\n"
        "X,A,B,5,0\n"
        "X,A,C,5,0\n"
    ).encode()
    with pytest.raises(Exception) as excinfo:
        parse_csv(payload)
    assert any("重复" in e for e in excinfo.value.errors)

    payload = (
        "voucher_no,catalog_a,catalog_b,minutes,locked\n"
        "X,A,B,10001,0\n"
    ).encode()
    with pytest.raises(Exception) as excinfo:
        parse_csv(payload)
    assert any("1～10000" in e for e in excinfo.value.errors)


def test_capacity_limit_rejected(app):
    rows = [(f"V{i}", f"a{i % 250}", f"b{i % 250}", 1, 0) for i in range(2000)]
    load_rows(app, rows)
    with app.app_context():
        conn = db_module.get_db()
        from app.services import DomainError, import_credentials

        extra = [
            {"voucher_no": "OVER", "catalog_a": "x1", "catalog_b": "x2",
             "minutes": 1, "locked": 0}
        ]
        with pytest.raises(DomainError):
            with db_module.transaction(conn):
                import_credentials(conn, extra, "merge")
        assert conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"] == 2000


TWO_PAIR_ROWS = [
    ("S-1", "A", "X", 5, 0),
    ("S-2", "B", "Y", 5, 0),
    ("S-3", "X", "Y", 4, 0),      # shared credential, cheapest single cut
    ("S-4", "C", "X", 5, 0),
    ("S-5", "D", "Y", 5, 0),
]


def test_two_pairs_share_credential_end_to_end(client, app, worker):
    load_rows(app, TWO_PAIR_ROWS)
    job = solve_and_drain(app, worker, ("A", "B"), ("C", "D"))
    assert job["status"] == "done"
    assert job["result_total"] == 4
    items = json.loads(job["result_json"])
    assert [i["voucher_no"] for i in items] == ["S-3"]

    with app.app_context():
        conn = db_module.get_db()
        plan_id = job["plan_id"]
    page = client.get(f"/plans/{plan_id}")
    body = page.get_data(as_text=True)
    assert "S-3" in body and "可以确认" in body

    resp = post(client, f"/plans/{plan_id}/confirm", token_path=f"/plans/{plan_id}")
    body = resp.get_data(as_text=True)
    assert "确认成功" in body
    with app.app_context():
        conn = db_module.get_db()
        row = conn.execute(
            "SELECT active FROM credentials WHERE voucher_no = 'S-3'"
        ).fetchone()
        assert row["active"] == 0
        plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        assert plan["status"] == "confirmed"
        # remaining pairs must be disconnected
        active = conn.execute(
            "SELECT catalog_a, catalog_b FROM credentials WHERE active = 1"
        ).fetchall()
        assert not _linked(active, "A", "B")
        assert not _linked(active, "C", "D")


def _linked(rows, a, b):
    adj = {}
    for row in rows:
        adj.setdefault(row[0], []).append(row[1])
        adj.setdefault(row[1], []).append(row[0])
    seen = {a}
    stack = [a]
    while stack:
        v = stack.pop()
        for u in adj.get(v, []):
            if u not in seen:
                seen.add(u)
                stack.append(u)
    return b in seen


def test_confirmation_rejected_after_edit_and_feed_unchanged(client, app, worker):
    rows = [
        ("E-1", "A", "B", 5, 0),
        ("E-2", "A", "B", 6, 0),
        ("E-3", "B", "C", 100, 0),
    ]
    load_rows(app, rows)
    job = solve_and_drain(app, worker, ("A", "C"))
    assert job["status"] == "done"
    plan_id = job["plan_id"]
    with app.app_context():
        conn = db_module.get_db()
        item_id = conn.execute(
            "SELECT credential_id FROM plan_items WHERE plan_id = ?", (plan_id,)
        ).fetchone()[0]

    # The plan is E-1 + E-2 (11) versus E-3 (100).  After solving, E-1's
    # minutes are corrected up to 200, so the cheapest list becomes E-3 (100)
    # and the old plan is no longer minimal.
    resp = post(
        client,
        f"/credentials/{item_id}/edit",
        {"minutes": "200", "locked": "0"},
        token_path="/credentials",
    )
    assert "已更新" in resp.get_data(as_text=True)

    page = client.get(f"/plans/{plan_id}")
    body = page.get_data(as_text=True)
    assert "暂不能确认" in body
    assert "已非最少" in body

    before = _feed_snapshot(app)
    resp = post(client, f"/plans/{plan_id}/confirm", token_path=f"/plans/{plan_id}")
    body = resp.get_data(as_text=True)
    assert "确认被拒绝" in body
    assert _feed_snapshot(app) == before
    with app.app_context():
        conn = db_module.get_db()
        plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        assert plan["status"] == "draft"


def _feed_snapshot(app):
    with app.app_context():
        conn = db_module.get_db()
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT voucher_no, minutes, locked, active FROM credentials ORDER BY id"
            )
        ]


def test_confirmation_rejected_when_item_locked_after_solve(client, app, worker):
    rows = [
        ("L-1", "A", "B", 5, 0),
        ("L-2", "B", "C", 10, 0),
    ]
    load_rows(app, rows)
    job = solve_and_drain(app, worker, ("A", "C"))
    plan_id = job["plan_id"]

    with app.app_context():
        conn = db_module.get_db()
        cid = conn.execute(
            "SELECT credential_id FROM plan_items WHERE plan_id = ?", (plan_id,)
        ).fetchone()[0]

    post(
        client,
        f"/credentials/{cid}/edit",
        {"minutes": "5", "locked": "1"},
        token_path="/credentials",
    )
    page = client.get(f"/plans/{plan_id}")
    assert "现已锁定" in page.get_data(as_text=True)

    before = _feed_snapshot(app)
    resp = post(client, f"/plans/{plan_id}/confirm", token_path=f"/plans/{plan_id}")
    assert "确认被拒绝" in resp.get_data(as_text=True)
    assert _feed_snapshot(app) == before


def test_no_solution_leaves_feed_unchanged(client, app, worker):
    rows = [
        ("N-1", "A", "B", 5, 1),
    ]
    load_rows(app, rows)
    job = solve_and_drain(app, worker, ("A", "B"))
    assert job["status"] == "nosolution"
    assert job["plan_id"] is None
    before = _feed_snapshot(app)
    page = client.get(f"/jobs/{job['id']}")
    assert "无解" in page.get_data(as_text=True)
    assert _feed_snapshot(app) == before


def test_preview_after_correction_that_keeps_plan_valid(client, app, worker):
    # Sole cheapest edge is untouched; another credential's minutes change.
    rows = [
        ("P-1", "A", "B", 5, 0),
        ("P-2", "A", "B", 9, 0),
        ("P-3", "B", "C", 8, 0),
    ]
    load_rows(app, rows)
    job = solve_and_drain(app, worker, ("A", "C"))
    plan_id = job["plan_id"]
    # Cutting A-B costs 5 + 9 = 14; cutting B-C costs 8, so B-C is optimal.
    assert job["result_total"] == 8
    with app.app_context():
        conn = db_module.get_db()
        listed = [
            row["voucher_no"]
            for row in conn.execute(
                "SELECT voucher_no FROM plan_items WHERE plan_id = ?", (plan_id,)
            )
        ]
    assert listed == ["P-3"]

    with app.app_context():
        conn = db_module.get_db()
        with db_module.transaction(conn):
            conn.execute("UPDATE credentials SET minutes = 4 WHERE voucher_no = 'P-1'")
            db_module.bump_version(conn)

    # Plan P-3 (8) is still the minimum: cutting A-B would cost 4 + 9 = 13.
    page = client.get(f"/plans/{plan_id}")
    assert "可以确认" in page.get_data(as_text=True)
    resp = post(client, f"/plans/{plan_id}/confirm", token_path=f"/plans/{plan_id}")
    assert "确认成功" in resp.get_data(as_text=True)


def test_solve_rejects_unknown_catalog_and_identical_pairs(client, app):
    load_rows(app, BASE_ROWS)
    resp = post(client, "/solve", {"p1a": "A", "p1b": "ZZ"}, token_path="/solve")
    assert "不存在" in resp.get_data(as_text=True)
    resp = post(client, "/solve", {"p1a": "A", "p1b": "A"}, token_path="/solve")
    assert "不能相同" in resp.get_data(as_text=True)
    resp = post(
        client,
        "/solve",
        {"p1a": "A", "p1b": "B", "p2a": "A", "p2b": "B"},
        token_path="/solve",
    )
    assert "同一对编号" in resp.get_data(as_text=True)


def test_restore_withdrawn_credential(client, app, worker):
    load_rows(app, BASE_ROWS)
    job = solve_and_drain(app, worker, ("A", "C"))
    plan_id = job["plan_id"]
    post(client, f"/plans/{plan_id}/confirm", token_path=f"/plans/{plan_id}")
    with app.app_context():
        conn = db_module.get_db()
        cid = conn.execute(
            "SELECT id FROM credentials WHERE voucher_no = 'V-3'"
        ).fetchone()[0]
        assert conn.execute(
            "SELECT active FROM credentials WHERE id = ?", (cid,)
        ).fetchone()["active"] == 0
    resp = post(client, f"/credentials/{cid}/restore", token_path="/credentials")
    assert "已恢复" in resp.get_data(as_text=True)
    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute(
            "SELECT active FROM credentials WHERE id = ?", (cid,)
        ).fetchone()["active"] == 1


def test_csrf_required(client, app):
    load_rows(app, BASE_ROWS)
    resp = client.post("/solve", data={"p1a": "A", "p1b": "B"})
    assert resp.status_code == 400


def test_plan_list_and_export(client, app, worker):
    load_rows(app, BASE_ROWS)
    job = solve_and_drain(app, worker, ("A", "C"))
    plan_id = job["plan_id"]
    body = client.get("/plans").get_data(as_text=True)
    assert f"/plans/{plan_id}" in body
    csv_resp = client.get(f"/plans/{plan_id}/items.csv")
    assert csv_resp.status_code == 200
    assert "voucher_no,catalog_a,catalog_b,minutes" in csv_resp.get_data(as_text=True)


def test_write_failure_rolls_back(client, app, worker, monkeypatch):
    load_rows(app, BASE_ROWS)
    job = solve_and_drain(app, worker, ("A", "C"))
    plan_id = job["plan_id"]
    before = _feed_snapshot(app)

    original_bump = db_module.bump_version

    calls = {"n": 0}

    def boom(conn):
        calls["n"] += 1
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("app.services.db_module.bump_version", boom)
    resp = post(client, f"/plans/{plan_id}/confirm", token_path=f"/plans/{plan_id}")
    assert "写入失败" in resp.get_data(as_text=True)
    assert _feed_snapshot(app) == before
    with app.app_context():
        conn = db_module.get_db()
        plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        assert plan["status"] == "draft"
    monkeypatch.setattr("app.services.db_module.bump_version", original_bump)


def test_edits_are_possible_while_solve_runs(client, app, worker, monkeypatch):
    """A running solve must not block correcting minutes or lock flags."""
    import threading
    import time

    import app.worker as worker_module

    load_rows(
        app,
        [
            ("C-1", "A", "B", 5, 0),
            ("C-2", "A", "B", 6, 0),
            ("C-3", "B", "C", 50, 0),
        ],
    )
    started = threading.Event()
    release = threading.Event()
    real_solve = worker_module.solve

    def slow_solve(edges, pair1, pair2=None, cancel_cb=None):
        started.set()
        assert release.wait(5), "test did not release the solve"
        return real_solve(edges, pair1, pair2, cancel_cb=cancel_cb)

    monkeypatch.setattr(worker_module, "solve", slow_solve)

    with app.app_context():
        conn = db_module.get_db()
        with db_module.transaction(conn):
            from app.services import enqueue_solve

            job_id = enqueue_solve(conn, ("A", "C"), None)

    thread = threading.Thread(target=worker.process_all)
    thread.start()
    try:
        assert started.wait(5), "solve never started"
        # While the solve is paused, the catalogue keeps working: this edit
        # uses the request thread's own connection.
        with app.app_context():
            conn = db_module.get_db()
            started_at = time.monotonic()
            with db_module.transaction(conn):
                conn.execute(
                    "UPDATE credentials SET minutes = 7, locked = 1 "
                    "WHERE voucher_no = 'C-2'"
                )
                db_module.bump_version(conn)
            assert time.monotonic() - started_at < 2.0
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()

    with app.app_context():
        conn = db_module.get_db()
        job = conn.execute("SELECT * FROM solve_jobs WHERE id = ?", (job_id,)).fetchone()
        plan = conn.execute("SELECT * FROM plans WHERE id = ?", (job["plan_id"],)).fetchone()
    # The snapshot was taken before the edit; the draft reflects the old data,
    # so it is stale and its confirmation will be revalidated.
    assert job["status"] == "done"
    assert plan["solved_version"] != _current_version(app)

    page = client.get(f"/plans/{plan['id']}").get_data(as_text=True)
    assert "供数在求解后已更正" in page
    # The listed C-3 (50) is now locked, so confirmation must be refused.
    before = _feed_snapshot(app)
    resp = post(client, f"/plans/{plan['id']}/confirm", token_path=f"/plans/{plan['id']}")
    assert "确认被拒绝" in resp.get_data(as_text=True)
    assert _feed_snapshot(app) == before


def _current_version(app):
    with app.app_context():
        return db_module.get_version(db_module.get_db())


def test_confirm_refuses_if_catalogue_moved_after_revalidation(app, worker, monkeypatch):
    """The final version check inside the write transaction is load-bearing."""
    from app import services as services_module
    from app.services import PlanNotConfirmable, confirm_plan

    load_rows(
        app,
        [
            ("R-1", "A", "B", 4, 0),
            ("R-2", "B", "C", 9, 0),
        ],
    )
    job = solve_and_drain(app, worker, ("A", "C"))
    plan_id = job["plan_id"]

    real_evaluate = services_module.evaluate_plan
    db_path = app.config["DB_PATH"]
    state = {"moved": False}

    def evaluate_then_edit(conn, plan):
        result = real_evaluate(conn, plan)
        if not state["moved"]:
            state["moved"] = True
            # Another request commits after revalidation but before our write.
            other = db_module.connect(db_path)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute(
                    "UPDATE credentials SET minutes = 8 WHERE voucher_no = 'R-2'"
                )
                other.execute(
                    "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
                    "WHERE key = 'data_version'"
                )
                other.execute("COMMIT")
            finally:
                other.close()
        return result

    monkeypatch.setattr(services_module, "evaluate_plan", evaluate_then_edit)

    with app.app_context():
        conn = db_module.get_db()
        with pytest.raises(PlanNotConfirmable):
            confirm_plan(conn, plan_id)
        plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        assert plan["status"] == "draft"
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM credentials WHERE active = 1"
        ).fetchone()["n"] == 2


def test_cap_from_config_is_enforced(client, app, worker):
    small = create_app(
        {
            "TESTING": True,
            "DB_PATH": app.config["DB_PATH"],
            "START_WORKER": False,
            "SECRET_KEY": "test-secret",
            "MAX_CREDENTIALS": 2,
        }
    )
    c = small.test_client()
    csv_text = (
        "voucher_no,catalog_a,catalog_b,minutes,locked\n"
        "Z-1,A,B,1,0\n"
        "Z-2,B,C,1,0\n"
        "Z-3,C,D,1,0\n"
    )
    resp = c.post(
        "/import",
        data={
            "csrf_token": csrf(c),
            "mode": "merge",
            "csv_file": (io.BytesIO(csv_text.encode()), "x.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "上限 2 份" in body
    with small.app_context():
        conn = db_module.get_db()
        assert conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"] == 0



def test_already_disconnected_pair_yields_empty_plan(client, app, worker):
    load_rows(
        app,
        [
            ("D-1", "A", "B", 4, 0),
            ("D-2", "C", "D", 6, 0),
        ],
    )
    job = solve_and_drain(app, worker, ("A", "C"))
    assert job["status"] == "done"
    assert job["result_total"] == 0
    plan_id = job["plan_id"]
    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM plan_items WHERE plan_id = ?", (plan_id,)
        ).fetchone()["n"] == 0
    resp = post(client, f"/plans/{plan_id}/confirm", token_path=f"/plans/{plan_id}")
    body = resp.get_data(as_text=True)
    assert "确认成功" in body and "无需撤下任何凭证" in body
    with app.app_context():
        conn = db_module.get_db()
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM credentials WHERE active = 1"
        ).fetchone()["n"] == 2
