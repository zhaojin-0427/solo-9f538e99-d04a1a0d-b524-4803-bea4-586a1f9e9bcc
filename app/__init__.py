from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from . import db as db_module
from .services import MAX_CREDENTIALS as MAX_SUPPORTED_CREDENTIALS

BASE_DIR = Path(__file__).resolve().parent.parent


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        DB_PATH=os.environ.get("CATALOG_DB", str(BASE_DIR / "data" / "catalog.db")),
        MAX_CREDENTIALS=min(
            int(os.environ.get("MAX_CREDENTIALS", str(MAX_SUPPORTED_CREDENTIALS))),
            MAX_SUPPORTED_CREDENTIALS,
        ),
        MAX_UPLOAD_BYTES=5 * 1024 * 1024,
        START_WORKER=True,
    )
    if config:
        app.config.update(config)
    app.config["MAX_CONTENT_LENGTH"] = app.config["MAX_UPLOAD_BYTES"]

    app.teardown_appcontext(db_module.close_db)
    with app.app_context():
        db_module.init_db()

    from .routes import bp

    app.register_blueprint(bp)

    if app.config["START_WORKER"]:
        from .worker import get_worker

        get_worker(app).ensure_started()

    return app
