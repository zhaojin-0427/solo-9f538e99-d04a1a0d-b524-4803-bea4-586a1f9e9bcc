from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app import db as db_module  # noqa: E402
from app.services import import_credentials  # noqa: E402
from app.worker import get_worker  # noqa: E402

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
FEED_GEN_RE = re.compile(r'name="feed_generation" value="([^"]+)"')


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "DB_PATH": str(tmp_path / "test.db"),
            "START_WORKER": False,
            "SECRET_KEY": "test-secret",
        }
    )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def conn(app):
    with app.app_context():
        yield db_module.get_db()


@pytest.fixture()
def worker(app):
    return get_worker(app)


def load_rows(app, rows):
    with app.app_context():
        c = db_module.get_db()
        normalized = [
            {
                "voucher_no": v,
                "catalog_a": a,
                "catalog_b": b,
                "minutes": m,
                "locked": l,
            }
            for v, a, b, m, l in rows
        ]
        with db_module.transaction(c):
            return import_credentials(c, normalized, "merge")


def csrf(client, path="/"):
    page = client.get(path)
    match = CSRF_RE.search(page.get_data(as_text=True))
    assert match, f"no CSRF token on {path}"
    return match.group(1)


def post(client, path, data=None, token_path="/"):
    token_page = client.get(token_path)
    page_text = token_page.get_data(as_text=True)
    match = CSRF_RE.search(page_text)
    assert match, f"no CSRF token on {token_path}"
    payload = {"csrf_token": match.group(1)}
    feed_match = FEED_GEN_RE.search(page_text)
    if feed_match is not None:
        payload["feed_generation"] = feed_match.group(1)
    payload.update(data or {})
    return client.post(path, data=payload, follow_redirects=True)


def solve_and_drain(app, worker, pair1, pair2=None):
    with app.app_context():
        c = db_module.get_db()
        with db_module.transaction(c):
            from app.services import enqueue_solve

            job_id = enqueue_solve(c, pair1, pair2)
    worker.process_all()
    with app.app_context():
        c = db_module.get_db()
        return c.execute("SELECT * FROM solve_jobs WHERE id = ?", (job_id,)).fetchone()
