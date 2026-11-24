from __future__ import annotations

import hmac
import io
import json
import sqlite3

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from . import db as db_module
from . import services
from .services import DomainError, ImportErrorList
from .worker import get_worker

bp = Blueprint("main", __name__)

SAMPLE_CSV = (
    "voucher_no,catalog_a,catalog_b,minutes,locked\n"
    "V-9001,CAT-A100,CAT-B200,40,0\n"
    "V-9002,CAT-A100,CAT-C300,75,0\n"
    "V-9003,CAT-B200,CAT-C300,25,0\n"
    "V-9004,CAT-B200,CAT-D400,60,1\n"
    "V-9005,CAT-C300,CAT-D400,55,0\n"
    "V-9006,CAT-A100,CAT-D400,90,0\n"
)


# ---------------------------------------------------------------------------
# CSRF (lightweight; protects this local tool from cross-site form posts)
# ---------------------------------------------------------------------------


def _csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        import secrets

        token = secrets.token_urlsafe(24)
        session["csrf_token"] = token
    return token


@bp.app_context_processor
def _inject_globals():
    return {"csrf_token": _csrf_token()}


@bp.before_request
def _check_csrf():
    if request.method == "POST":
        expected = session.get("csrf_token")
        supplied = request.form.get("csrf_token", "")
        if not expected or not hmac.compare_digest(str(supplied), str(expected)):
            abort(400, description="表单已过期，请刷新页面后重试。")


def _safe_next(fallback: str) -> str:
    """Only allow local redirect targets supplied by a form field."""
    target = request.form.get("next") or ""
    if target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


def _template_context(**extra):
    conn = db_module.get_db()
    stats = conn.execute(
        """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(active), 0) AS active,
               COALESCE(SUM(CASE WHEN active = 1 AND locked = 1 THEN 1 ELSE 0 END), 0) AS locked
        FROM credentials
        """
    ).fetchone()
    draft = conn.execute(
        "SELECT COUNT(*) AS n FROM plans WHERE status = 'draft'"
    ).fetchone()["n"]
    context = {
        "stats": {
            "total": stats["total"],
            "active": stats["active"],
            "locked": stats["locked"],
            "drafts": draft,
        },
        "max_credentials": current_app.config["MAX_CREDENTIALS"],
    }
    context.update(extra)
    return context


# ---------------------------------------------------------------------------
# Overview / import
# ---------------------------------------------------------------------------


@bp.get("/")
def index():
    conn = db_module.get_db()
    jobs = conn.execute(
        "SELECT * FROM solve_jobs ORDER BY id DESC LIMIT 8"
    ).fetchall()
    plans = conn.execute(
        "SELECT * FROM plans ORDER BY (status = 'confirmed'), id DESC LIMIT 8"
    ).fetchall()
    return render_template(
        "index.html", **_template_context(jobs=jobs, plans=plans)
    )


@bp.get("/sample.csv")
def sample_csv():
    data = io.BytesIO(SAMPLE_CSV.encode("utf-8"))
    return send_file(
        data,
        mimetype="text/csv; charset=utf-8",
        as_attachment=True,
        download_name="credentials-sample.csv",
    )


@bp.post("/import")
def import_csv():
    conn = db_module.get_db()
    upload = request.files.get("csv_file")
    mode = request.form.get("mode", "merge")
    if upload is None or not upload.filename:
        flash("请选择要导入的 CSV 文件。", "error")
        return redirect(url_for("main.index"))
    try:
        rows = services.parse_csv(upload.read())
        with db_module.transaction(conn):
            result = services.import_credentials(
                conn, rows, mode, limit=current_app.config["MAX_CREDENTIALS"]
            )
    except (ImportErrorList, DomainError) as exc:
        messages = getattr(exc, "errors", [str(exc)])
        for message in messages:
            flash(message, "error")
        return redirect(url_for("main.index"))
    except sqlite3.Error:
        current_app.logger.exception("CSV import failed")
        flash("写入失败，供数未发生任何变化。", "error")
        return redirect(url_for("main.index"))

    if mode == "replace":
        flash(f"已替换供数：共 {result.total} 份凭证。", "ok")
    else:
        flash(
            f"导入完成：新增 {result.inserted} 份，更新 {result.updated} 份，"
            f"当前共 {result.total} 份凭证。",
            "ok",
        )
    return redirect(url_for("main.index"))


@bp.post("/demo-data")
def demo_data():
    conn = db_module.get_db()
    try:
        with db_module.transaction(conn):
            total = services.seed_demo(conn)
    except DomainError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.index"))
    except sqlite3.Error:
        current_app.logger.exception("demo data load failed")
        flash("写入失败，供数未发生任何变化。", "error")
        return redirect(url_for("main.index"))
    flash(f"已载入示例供数，共 {total} 份凭证。", "ok")
    return redirect(url_for("main.index"))


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@bp.app_errorhandler(413)
def too_large(_error):
    flash("文件超过 5 MB 上限，未做任何改动。", "error")
    return redirect(url_for("main.index"))


@bp.app_errorhandler(400)
def bad_request(error):
    message = getattr(error, "description", None) or "请求无效。"
    return (
        render_template("error.html", **_template_context(message=message)),
        400,
    )


@bp.app_errorhandler(404)
def not_found(_error):
    return (
        render_template("error.html", **_template_context(message="页面或记录不存在。")),
        404,
    )


@bp.get("/credentials")
def credentials():
    conn = db_module.get_db()
    q = request.args.get("q", "").strip()
    state = request.args.get("state", "all")
    page = max(1, request.args.get("page", type=int) or 1)
    per_page = 50

    where = []
    params: list = []
    if q:
        where.append(
            "(voucher_no LIKE ? OR catalog_a LIKE ? OR catalog_b LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like])
    if state == "active":
        where.append("active = 1")
    elif state == "withdrawn":
        where.append("active = 0")
    elif state == "locked":
        where.append("active = 1 AND locked = 1")
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM credentials {clause}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT * FROM credentials {clause}
        ORDER BY active DESC, voucher_no
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    pages = max(1, (total + per_page - 1) // per_page)
    return render_template(
        "credentials.html",
        **_template_context(
            rows=rows,
            q=q,
            state=state,
            page=page,
            pages=pages,
            total=total,
        ),
    )


@bp.post("/credentials/<int:credential_id>/edit")
def edit_credential(credential_id: int):
    conn = db_module.get_db()
    raw_minutes = request.form.get("minutes", "")
    locked = 1 if request.form.get("locked") == "1" else 0
    try:
        minutes = services.parse_minutes(raw_minutes)
    except ValueError as exc:
        flash(f"未保存：{exc}", "error")
        return redirect(_safe_next(url_for("main.credentials")))

    try:
        with db_module.transaction(conn):
            cur = conn.execute(
                """
                UPDATE credentials
                SET minutes = ?, locked = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (minutes, locked, credential_id),
            )
            if cur.rowcount != 1:
                raise DomainError("凭证不存在。")
            db_module.bump_version(conn)
    except DomainError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.credentials"))
    except sqlite3.Error:
        current_app.logger.exception("credential edit failed")
        flash("写入失败，供数未发生任何变化。", "error")
        return redirect(url_for("main.credentials"))

    flash("已更新复核工时与锁定状态。", "ok")
    return redirect(_safe_next(url_for("main.credentials")))


@bp.post("/credentials/<int:credential_id>/restore")
def restore_credential(credential_id: int):
    conn = db_module.get_db()
    try:
        with db_module.transaction(conn):
            cur = conn.execute(
                "UPDATE credentials SET active = 1, updated_at = datetime('now') "
                "WHERE id = ? AND active = 0",
                (credential_id,),
            )
            if cur.rowcount != 1:
                raise DomainError("该凭证已在供数中。")
            db_module.bump_version(conn)
    except DomainError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.credentials", state="withdrawn"))
    except sqlite3.Error:
        current_app.logger.exception("credential restore failed")
        flash("写入失败，供数未发生任何变化。", "error")
        return redirect(url_for("main.credentials", state="withdrawn"))
    flash("已恢复到供数清单。", "ok")
    return redirect(url_for("main.credentials", state="withdrawn"))


# ---------------------------------------------------------------------------
# Solve
# ---------------------------------------------------------------------------


@bp.get("/solve")
def solve_page():
    conn = db_module.get_db()
    numbers = services.catalog_numbers(conn)
    return render_template(
        "solve.html", **_template_context(numbers=numbers)
    )


@bp.post("/solve")
def solve_submit():
    conn = db_module.get_db()
    try:
        pair1 = services.normalize_pair(request.form.get("p1a", ""), request.form.get("p1b", ""))
        raw2 = [request.form.get("p2a", "").strip(), request.form.get("p2b", "").strip()]
        pair2 = None
        if any(raw2):
            if not all(raw2):
                raise DomainError("第二对要么两个编号都选，要么都不选。")
            pair2 = services.normalize_pair(raw2[0], raw2[1])
        services.validate_pairs(conn, pair1, pair2)
        with db_module.transaction(conn):
            job_id = services.enqueue_solve(conn, pair1, pair2)
    except DomainError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.solve_page"))
    except sqlite3.Error:
        current_app.logger.exception("enqueue solve failed")
        flash("写入失败，未能创建求解任务；供数未发生任何变化。", "error")
        return redirect(url_for("main.solve_page"))

    get_worker(current_app._get_current_object()).notify()
    return redirect(url_for("main.job_detail", job_id=job_id))


@bp.get("/jobs/<int:job_id>")
def job_detail(job_id: int):
    conn = db_module.get_db()
    job = conn.execute("SELECT * FROM solve_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        abort(404)
    plan = None
    result_count = 0
    if job["plan_id"]:
        plan = conn.execute("SELECT * FROM plans WHERE id = ?", (job["plan_id"],)).fetchone()
    if job["result_json"]:
        result_count = len(json.loads(job["result_json"]))
    return render_template(
        "job.html",
        **_template_context(job=job, plan=plan, result_count=result_count),
    )


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def _plan_rows(conn, status: str):
    return conn.execute(
        "SELECT * FROM plans WHERE status = ? ORDER BY id DESC", (status,)
    ).fetchall()


@bp.get("/plans")
def plans():
    conn = db_module.get_db()
    return render_template(
        "plans.html",
        **_template_context(
            drafts=_plan_rows(conn, "draft"),
            confirmed=_plan_rows(conn, "confirmed"),
        ),
    )


@bp.get("/plans/<int:plan_id>")
def plan_detail(plan_id: int):
    conn = db_module.get_db()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if plan is None:
        abort(404)
    items = services.plan_items(conn, plan_id)
    evaluation = (
        services.evaluate_plan(conn, plan) if plan["status"] == "draft" else None
    )
    return render_template(
        "plan_detail.html",
        **_template_context(
            plan=plan,
            items=items,
            evaluation=evaluation,
            listed_ids={item["credential_id"] for item in items},
        ),
    )


@bp.post("/plans/<int:plan_id>/confirm")
def confirm(plan_id: int):
    conn = db_module.get_db()
    try:
        withdrawn = services.confirm_plan(conn, plan_id)
    except DomainError as exc:
        flash(f"{exc} 供数清单未发生任何变化。", "error")
        return redirect(url_for("main.plan_detail", plan_id=plan_id))
    except sqlite3.Error:
        current_app.logger.exception("plan confirmation failed")
        flash("写入失败，供数清单未发生任何变化。", "error")
        return redirect(url_for("main.plan_detail", plan_id=plan_id))

    if withdrawn:
        flash(f"确认成功：{withdrawn} 份凭证已从供数清单撤下。", "ok")
    else:
        flash("确认成功：所选编号对当前已不相连，无需撤下任何凭证。", "ok")
    return redirect(url_for("main.plan_detail", plan_id=plan_id))


@bp.get("/plans/<int:plan_id>/items.csv")
def plan_items_csv(plan_id: int):
    conn = db_module.get_db()
    plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if plan is None:
        abort(404)
    items = services.plan_items(conn, plan_id)
    out = io.StringIO()
    out.write("voucher_no,catalog_a,catalog_b,minutes\n")
    for item in items:
        out.write(
            f"{item['voucher_no']},{item['catalog_a']},{item['catalog_b']},{item['minutes']}\n"
        )
    data = io.BytesIO(out.getvalue().encode("utf-8-sig"))
    return send_file(
        data,
        mimetype="text/csv; charset=utf-8",
        as_attachment=True,
        download_name=f"plan-{plan_id}.csv",
    )


@bp.get("/help")
def help_page():
    return render_template("help.html", **_template_context())
