"""Background solve worker.

The worker reads a consistent snapshot of the catalogue, releases the
database, computes the optimal list, and only then writes the draft plan.
Edits to review minutes or lock flags made while a job is running therefore
never block, and are picked up by the confirmation-time revalidation.

Replacement semantics ("替换全部供数"):

* a successful replace marks every queued/running job of the old feed
  ``void`` inside the same transaction and bumps the feed generation;
* the worker owns one thread (at most one solve runs at a time) and is given
  a cancel event, so the in-flight old solve is aborted as soon as the
  replacement commits and the next (new-feed) job can take over;
* a late result can never create a plan: the write path re-checks the job
  status, and void jobs are never requeued -- including after a restart.
"""

from __future__ import annotations

import threading

from . import db as db_module
from . import services
from .solver import SolveCancelled, solve


class SolverWorker:
    def __init__(self, app) -> None:
        self.app = app
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cancel_current = threading.Event()
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------
    def ensure_started(self) -> None:
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._cancel_current.clear()
            self._thread = threading.Thread(
                target=self._loop, name="catalog-solver", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def notify(self) -> None:
        self._wake.set()

    def request_cancel(self) -> None:
        """Ask the currently running solve to abort.

        Only the replace-all flow calls this; the event is cleared by the
        worker itself right before it starts the next job, so a replacement
        can never cancel jobs created afterwards.
        """
        self._cancel_current.set()
        self._wake.set()

    def _loop(self) -> None:
        # Single worker process: only jobs left "running" by a previous
        # process crash are requeued.  Void jobs stay void forever and are
        # never executed again.
        try:
            with self.app.app_context():
                conn = db_module.get_db()
                with db_module.transaction(conn):
                    conn.execute(
                        "UPDATE solve_jobs SET status = 'queued', started_at = NULL "
                        "WHERE status = 'running'"
                    )
        except Exception:  # pragma: no cover - startup best effort
            self.app.logger.exception("could not requeue interrupted solve jobs")

        while not self._stop.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                while self.process_next():
                    pass
            except Exception:  # pragma: no cover - defensive
                self.app.logger.exception("solver worker iteration failed")

    # -- job processing ----------------------------------------------------
    def process_next(self) -> bool:
        with self.app.app_context():
            conn = db_module.get_db()
            job = self._claim(conn)
            if job is None:
                return False
            try:
                self._run_job(conn, job)
            except SolveCancelled:
                self._mark_void_if_needed(conn, job["id"])
            except Exception:  # pragma: no cover - defensive
                self.app.logger.exception("solve job %s failed", job["id"])
                self._fail(conn, job["id"], "求解失败（内部错误），供数未发生任何变化。")
            return True

    def process_all(self) -> None:
        while self.process_next():
            pass

    def _claim(self, conn):
        with db_module.transaction(conn):
            row = conn.execute(
                "SELECT * FROM solve_jobs WHERE status = 'queued' "
                "ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE solve_jobs SET status = 'running', started_at = datetime('now') "
                "WHERE id = ? AND status = 'queued'",
                (row["id"],),
            )
            # A new job belongs to the current feed: any earlier replacement
            # must not abort it.
            self._cancel_current.clear()
            return row

    def _run_job(self, conn, job) -> None:
        pair1 = (job["pair1_a"], job["pair1_b"])
        pair2 = (
            (job["pair2_a"], job["pair2_b"])
            if job["pair2_a"] is not None and job["pair2_b"] is not None
            else None
        )

        # Consistent snapshot; the connection is not held during the solve.
        with db_module.read_snapshot(conn):
            edges = services.active_edges(conn)
            version = db_module.get_version(conn)

        cancel_cb = self._cancel_current.is_set
        result = solve(edges, pair1, pair2, cancel_cb=cancel_cb)

        with db_module.transaction(conn):
            current = conn.execute(
                "SELECT status FROM solve_jobs WHERE id = ?", (job["id"],)
            ).fetchone()
            # The replacement committed while we were computing: the job has
            # been voided, so no plan may be written even though we have a
            # result.  This is the late-result guard for every race the
            # cooperative cancel did not catch in time.
            if current is None or current["status"] != "running":
                return
            if result is None:
                conn.execute(
                    "UPDATE solve_jobs SET status = 'nosolution', "
                    "finished_at = datetime('now'), "
                    "message = '在锁定凭证不可停用的前提下，所有有效凭证都用上也无法分离所选编号对。' "
                    "WHERE id = ?",
                    (job["id"],),
                )
                return
            plan_id = self._create_plan(conn, job, pair1, pair2, result, version)
            conn.execute(
                "UPDATE solve_jobs SET status = 'done', finished_at = datetime('now'), "
                "result_total = ?, result_json = ?, plan_id = ? WHERE id = ?",
                (result.total, services.result_payload(result), plan_id, job["id"]),
            )

    def _mark_void_if_needed(self, conn, job_id: int) -> None:
        try:
            with db_module.transaction(conn):
                conn.execute(
                    "UPDATE solve_jobs SET status = 'void', "
                    "finished_at = COALESCE(finished_at, datetime('now')), "
                    "message = COALESCE(message, '供数已被整批替换，该任务作废。') "
                    "WHERE id = ? AND status = 'running'",
                    (job_id,),
                )
        except Exception:  # pragma: no cover - defensive
            self.app.logger.exception("could not finalize cancelled job %s", job_id)

    @staticmethod
    def _create_plan(conn, job, pair1, pair2, result, version) -> int:
        cur = conn.execute(
            """
            INSERT INTO plans(status, pair1_a, pair1_b, pair2_a, pair2_b,
                              plan_total, solved_version)
            VALUES ('draft', ?, ?, ?, ?, ?, ?)
            """,
            (
                pair1[0],
                pair1[1],
                pair2[0] if pair2 else None,
                pair2[1] if pair2 else None,
                result.total,
                version,
            ),
        )
        plan_id = int(cur.lastrowid)
        for edge in result.items:
            conn.execute(
                """
                INSERT INTO plan_items(plan_id, credential_id, voucher_no,
                                       catalog_a, catalog_b, minutes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    edge.credential_id,
                    edge.voucher_no,
                    edge.catalog_a,
                    edge.catalog_b,
                    edge.minutes,
                ),
            )
        return plan_id

    @staticmethod
    def _fail(conn, job_id: int, message: str) -> None:
        try:
            with db_module.transaction(conn):
                conn.execute(
                    "UPDATE solve_jobs SET status = 'error', finished_at = datetime('now'), "
                    "message = ? WHERE id = ?",
                    (message, job_id),
                )
        except Exception:  # pragma: no cover
            pass


def get_worker(app) -> SolverWorker:
    worker = app.extensions.get("solver_worker")
    if worker is None:
        worker = SolverWorker(app)
        app.extensions["solver_worker"] = worker
    return worker
