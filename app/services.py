"""Domain logic: CSV import, solve-job creation, plan evaluation/confirmation."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable

from . import db as db_module
from .solver import Edge, SolveResult, solve

MIN_MINUTES = 1
MAX_MINUTES = 10_000
MAX_CREDENTIALS = 2_000

CSV_COLUMNS = ("voucher_no", "catalog_a", "catalog_b", "minutes", "locked")

DEMO_ROWS = [
    ("V-1001", "CAT-A100", "CAT-B200", 40, 0),
    ("V-1002", "CAT-A100", "CAT-C300", 75, 0),
    ("V-1003", "CAT-B200", "CAT-C300", 25, 0),
    ("V-1004", "CAT-B200", "CAT-D400", 60, 1),
    ("V-1005", "CAT-C300", "CAT-D400", 55, 0),
    ("V-1006", "CAT-A100", "CAT-D400", 90, 0),
    ("V-1007", "CAT-E500", "CAT-F600", 30, 0),
    ("V-1008", "CAT-F600", "CAT-G700", 45, 0),
    ("V-1009", "CAT-E500", "CAT-G700", 80, 0),
    ("V-1010", "CAT-F600", "CAT-H800", 20, 1),
    ("V-1011", "CAT-G700", "CAT-H800", 35, 0),
]


class DomainError(Exception):
    """User-facing error; nothing is written."""


class ImportErrorList(DomainError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("导入未完成")
        self.errors = errors


@dataclass
class ImportResult:
    inserted: int = 0
    updated: int = 0
    total: int = 0
    mode: str = "merge"
    voided_jobs: int = 0


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------


def _parse_locked(raw: str) -> bool:
    text = (raw or "").strip().lower()
    if text in ("1", "true", "yes", "y", "是", "锁定"):
        return True
    if text in ("0", "false", "no", "n", "否", "有效", ""):
        return False
    raise ValueError(f"锁定制「{raw}」无法识别（用 0/1 或 true/false）")


def parse_minutes(raw: str) -> int:
    text = (raw or "").strip()
    if not text:
        raise ValueError("缺少复核工时")
    if not (text.isascii() and text.isdigit()):
        raise ValueError("工时必须是 1～10000 的整数")
    value = int(text)
    if not (MIN_MINUTES <= value <= MAX_MINUTES):
        raise ValueError("工时必须是 1～10000 的整数")
    return value


def _clean_catalog(raw: str, label: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise ValueError(f"{label}不能为空")
    if len(text) > 120:
        raise ValueError(f"{label}过长")
    return text


def parse_csv(content: bytes) -> list[dict]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportErrorList(["文件不是 UTF-8 编码。"]) from exc

    reader = csv.DictReader(io.StringIO(text))
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in CSV_COLUMNS if c not in headers]
    if missing:
        raise ImportErrorList(
            ["缺少必需列：" + "、".join(missing) + "（表头应为 " + ",".join(CSV_COLUMNS) + "）"]
        )

    errors: list[str] = []
    rows: list[dict] = []
    seen_vouchers: set[str] = set()

    for line_no, raw_row in enumerate(reader, start=2):
        normalized = {
            (k or "").strip().lower(): (v or "") for k, v in raw_row.items() if k
        }
        if not any(str(v).strip() for v in normalized.values()):
            continue
        try:
            voucher_no = (normalized.get("voucher_no") or "").strip()
            if not voucher_no:
                raise ValueError("凭证号不能为空")
            if len(voucher_no) > 120:
                raise ValueError("凭证号过长")
            if voucher_no in seen_vouchers:
                raise ValueError("文件内凭证号重复")
            catalog_a = _clean_catalog(normalized.get("catalog_a"), "编目号 A")
            catalog_b = _clean_catalog(normalized.get("catalog_b"), "编目号 B")
            if catalog_a == catalog_b:
                raise ValueError("两端编目号不能相同")
            minutes = parse_minutes(normalized.get("minutes"))
            locked = _parse_locked(normalized.get("locked"))
        except ValueError as exc:
            errors.append(f"第 {line_no} 行：{exc}")
            if len(errors) >= 30:
                errors.append("错误过多，仅显示前 30 条。")
                break
            continue
        seen_vouchers.add(voucher_no)
        rows.append(
            {
                "voucher_no": voucher_no,
                "catalog_a": catalog_a,
                "catalog_b": catalog_b,
                "minutes": minutes,
                "locked": 1 if locked else 0,
            }
        )
    if errors:
        raise ImportErrorList(errors)
    if not rows:
        raise ImportErrorList(["CSV 中没有可导入的凭证。"])
    return rows


def import_credentials(
    conn: sqlite3.Connection,
    rows: list[dict],
    mode: str,
    limit: int = MAX_CREDENTIALS,
) -> ImportResult:
    """Import inside an already-open transaction.  Raises on any problem."""
    if mode not in ("merge", "replace"):
        raise DomainError("未知的导入方式")
    if len(rows) > limit:
        raise DomainError(f"最多支持 {limit} 份凭证，文件含 {len(rows)} 行。")

    existing = {
        row["voucher_no"]: row
        for row in conn.execute(
            "SELECT id, voucher_no, minutes, locked FROM credentials"
        )
    }

    if mode == "replace":
        conn.execute("DELETE FROM plan_items")
        conn.execute("DELETE FROM plans")
        conn.execute("DELETE FROM credentials")
        # Every unfinished job belongs to the superseded feed: mark them void
        # so a running/queued solve can never write a plan against the new
        # feed, and a restart will not pick them up again.
        cur = conn.execute(
            "UPDATE solve_jobs SET status = 'void', "
            "finished_at = datetime('now'), "
            "message = '供数已被整批替换，该任务作废。' "
            "WHERE status IN ('queued', 'running')"
        )
        voided_jobs = cur.rowcount
        db_module.bump_feed_generation(conn)
        result = ImportResult(
            inserted=len(rows),
            updated=0,
            total=len(rows),
            mode=mode,
            voided_jobs=voided_jobs,
        )
    else:
        new_vouchers = [r["voucher_no"] for r in rows if r["voucher_no"] not in existing]
        total_after = len(existing) + len(new_vouchers)
        if total_after > limit:
            raise DomainError(
                f"导入后共 {total_after} 份凭证，超过上限 {limit} 份。"
            )
        result = ImportResult(
            inserted=len(new_vouchers),
            updated=len(rows) - len(new_vouchers),
            total=total_after,
            mode=mode,
        )

    for row in rows:
        conn.execute(
            """
            INSERT INTO credentials(voucher_no, catalog_a, catalog_b, minutes, locked, active)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(voucher_no) DO UPDATE SET
                catalog_a = excluded.catalog_a,
                catalog_b = excluded.catalog_b,
                minutes   = excluded.minutes,
                locked    = excluded.locked,
                updated_at = datetime('now')
            """,
            (
                row["voucher_no"],
                row["catalog_a"],
                row["catalog_b"],
                row["minutes"],
                row["locked"],
            ),
        )
    db_module.bump_version(conn)
    return result


# ---------------------------------------------------------------------------
# Shared read helpers
# ---------------------------------------------------------------------------


def active_edges(conn: sqlite3.Connection) -> list[Edge]:
    rows = conn.execute(
        """
        SELECT id, voucher_no, catalog_a, catalog_b, minutes, locked
        FROM credentials WHERE active = 1
        ORDER BY id
        """
    ).fetchall()
    return [
        Edge(
            credential_id=row["id"],
            voucher_no=row["voucher_no"],
            catalog_a=row["catalog_a"],
            catalog_b=row["catalog_b"],
            minutes=row["minutes"],
            locked=bool(row["locked"]),
        )
        for row in rows
    ]


def catalog_numbers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT catalog_a AS n FROM credentials
        UNION SELECT catalog_b AS n FROM credentials ORDER BY n
        """
    ).fetchall()
    return [row["n"] for row in rows]


# ---------------------------------------------------------------------------
# Pair validation and solve jobs
# ---------------------------------------------------------------------------


def normalize_pair(a: str, b: str) -> tuple[str, str] | None:
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        raise DomainError("每一对需要两个编目号。")
    if a == b:
        raise DomainError("同一对的两个编目号不能相同。")
    return (a, b) if a < b else (b, a)


def validate_pairs(
    conn: sqlite3.Connection,
    pair1: tuple[str, str],
    pair2: tuple[str, str] | None,
) -> None:
    known = set(catalog_numbers(conn))
    for pair in filter(None, (pair1, pair2)):
        for number in pair:
            if number not in known:
                raise DomainError(f"编目号「{number}」不存在，请先导入对应凭证。")
    if pair2 is not None and set(pair1) == set(pair2):
        raise DomainError("两对确认不同的藏品不能是同一对编号。")


def enqueue_solve(
    conn: sqlite3.Connection,
    pair1: tuple[str, str],
    pair2: tuple[str, str] | None,
    feed_generation: int | None = None,
) -> int:
    version = db_module.get_version(conn)
    if feed_generation is None:
        feed_generation = db_module.get_feed_generation(conn)
    cur = conn.execute(
        """
        INSERT INTO solve_jobs(pair1_a, pair1_b, pair2_a, pair2_b,
                               data_version, feed_generation)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            pair1[0],
            pair1[1],
            pair2[0] if pair2 else None,
            pair2[1] if pair2 else None,
            version,
            feed_generation,
        ),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Plan evaluation (preview and confirmation share this logic)
# ---------------------------------------------------------------------------


def _pairs_of(plan: sqlite3.Row) -> tuple[tuple[str, str], tuple[str, str] | None]:
    pair1 = (plan["pair1_a"], plan["pair1_b"])
    pair2 = (
        (plan["pair2_a"], plan["pair2_b"])
        if plan["pair2_a"] is not None and plan["pair2_b"] is not None
        else None
    )
    return pair1, pair2


def _connected(edges: Iterable[Edge], removed_ids: set[int], a: str, b: str) -> bool:
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for edge in edges:
        if edge.credential_id in removed_ids:
            continue
        union(edge.catalog_a, edge.catalog_b)
    return find(a) == find(b)


def plan_items(conn: sqlite3.Connection, plan_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT pi.*, c.active AS now_active, c.locked AS now_locked,
               c.minutes AS now_minutes, c.voucher_no AS now_voucher_no
        FROM plan_items pi
        LEFT JOIN credentials c ON c.id = pi.credential_id
        WHERE pi.plan_id = ?
        ORDER BY pi.voucher_no
        """,
        (plan_id,),
    ).fetchall()


@dataclass
class PlanEvaluation:
    feasible: bool
    separates: bool
    is_minimum: bool
    live_total: int
    optimum_total: int | None
    optimum_items: tuple[Edge, ...] = ()
    current_version: int = 0
    stale: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def can_confirm(self) -> bool:
        return self.feasible and self.separates and self.is_minimum


def evaluate_plan(conn: sqlite3.Connection, plan: sqlite3.Row) -> PlanEvaluation:
    items = plan_items(conn, plan["id"])
    edges = active_edges(conn)
    active_ids = {edge.credential_id for edge in edges}
    locked_ids = {edge.credential_id for edge in edges if edge.locked}
    by_id = {edge.credential_id: edge for edge in edges}
    current_version = db_module.get_version(conn)

    problems: list[str] = []
    live_total = 0
    listed_ids: list[int] = []
    for item in items:
        cid = item["credential_id"]
        listed_ids.append(cid)
        if cid not in active_ids:
            problems.append(f"凭证 {item['voucher_no']} 已停用或已从供数中移除。")
            continue
        if cid in locked_ids:
            problems.append(f"凭证 {item['voucher_no']} 现已锁定，不可停用。")
        live_total += by_id[cid].minutes

    feasible = not problems
    pair1, pair2 = _pairs_of(plan)
    separates = feasible and all(
        not _connected(edges, set(listed_ids), pair[0], pair[1])
        for pair in filter(None, (pair1, pair2))
    )
    if feasible and not separates:
        problems.append("该清单已不能分离所选编号对。")

    result: SolveResult | None = solve(edges, pair1, pair2)
    optimum_total = None if result is None else result.total
    optimum_items = () if result is None else result.items
    if result is None:
        problems.append("在锁定凭证不可停用的前提下无解。")

    is_minimum = (
        feasible
        and separates
        and optimum_total is not None
        and live_total == optimum_total
    )
    if (
        feasible
        and separates
        and optimum_total is not None
        and live_total != optimum_total
    ):
        problems.append(
            f"清单当前总工时 {live_total} 分钟，已非最少（当前最少 {optimum_total} 分钟）。"
        )

    return PlanEvaluation(
        feasible=feasible,
        separates=separates,
        is_minimum=is_minimum,
        live_total=live_total,
        optimum_total=optimum_total,
        optimum_items=tuple(optimum_items),
        current_version=current_version,
        stale=plan["solved_version"] != current_version,
        problems=problems,
    )


class PlanNotConfirmable(DomainError):
    """Confirmation was refused; nothing was written."""


def confirm_plan(conn: sqlite3.Connection, plan_id: int) -> int:
    """Revalidate and apply a draft plan as one atomic unit.

    The expensive revalidation runs on a read snapshot so the write lock is
    only held for the short update.  A version check inside the write
    transaction guarantees the snapshot was still current; if the catalogue
    changed in between, confirmation is refused and the feed is untouched.

    Returns the number of withdrawn credentials.  Raises DomainError (no
    mutation) when revalidation fails or the catalogue moves concurrently.
    """
    with db_module.read_snapshot(conn):
        plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if plan is None:
            raise DomainError("清单不存在。")
        if plan["status"] != "draft":
            raise DomainError("该清单已确认，不能重复确认。")
        evaluation = evaluate_plan(conn, plan)

    if not evaluation.can_confirm:
        raise PlanNotConfirmable("确认被拒绝：" + " ".join(evaluation.problems))

    items = plan_items(conn, plan_id)
    with db_module.transaction(conn):
        if db_module.get_version(conn) != evaluation.current_version:
            raise PlanNotConfirmable(
                "确认被拒绝：供数在确认期间发生变化，请重新打开清单后再试。"
            )
        for item in items:
            cur = conn.execute(
                """
                UPDATE credentials SET active = 0, updated_at = datetime('now')
                WHERE id = ? AND active = 1 AND locked = 0
                """,
                (item["credential_id"],),
            )
            if cur.rowcount != 1:  # pragma: no cover - version check guards this
                raise PlanNotConfirmable("确认被拒绝：供数在确认期间发生变化。")

        version = db_module.bump_version(conn)
        cur = conn.execute(
            """
            UPDATE plans
            SET status = 'confirmed', confirmed_at = datetime('now'),
                confirmed_version = ?, plan_total = ?
            WHERE id = ? AND status = 'draft'
            """,
            (version, evaluation.live_total, plan_id),
        )
        if cur.rowcount != 1:  # pragma: no cover - status checked above
            raise PlanNotConfirmable("确认被拒绝：清单状态已变化。")
    return len(items)


def result_payload(result: SolveResult) -> str:
    return json.dumps(
        [
            {
                "credential_id": e.credential_id,
                "voucher_no": e.voucher_no,
                "catalog_a": e.catalog_a,
                "catalog_b": e.catalog_b,
                "minutes": e.minutes,
                "locked": e.locked,
            }
            for e in result.items
        ],
        ensure_ascii=False,
    )


def seed_demo(conn: sqlite3.Connection) -> int:
    count = conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"]
    if count:
        raise DomainError("供数中已有凭证，未载入示例数据。")
    rows = [
        {
            "voucher_no": v,
            "catalog_a": a,
            "catalog_b": b,
            "minutes": m,
            "locked": l,
        }
        for v, a, b, m, l in DEMO_ROWS
    ]
    result = import_credentials(conn, rows, "merge")
    return result.total
