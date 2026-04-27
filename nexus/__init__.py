from __future__ import annotations

import os
import traceback
import uuid

from flask import Flask
from flask import render_template, request
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()


def create_app() -> Flask:
    """
    Flask app factory.
    """
    app = Flask(__name__, template_folder="templates", static_folder="static")

    # Core config
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///nexus_dev.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # LLM config (OpenAI-compatible gateway)
    app.config["LLM_BASE_URL_V1"] = os.getenv("LLM_BASE_URL_V1", "https://eclipse.mestredoblack.pro/v1")
    app.config["LLM_API_KEY"] = os.getenv("LLM_API_KEY", "")
    app.config["LLM_DEFAULT_MODEL"] = os.getenv("LLM_DEFAULT_MODEL", "deepseek-chat")

    # RQ / Redis
    app.config["REDIS_URL"] = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # Master admin (hard rule)
    app.config["MASTER_ADMIN_EMAIL"] = os.getenv("MASTER_ADMIN_EMAIL", "asafetork@gmail.com")

    # Stripe
    app.config["STRIPE_SECRET_KEY"] = os.getenv("STRIPE_SECRET_KEY", "")
    app.config["STRIPE_WEBHOOK_SECRET"] = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    app.config["STRIPE_PRICE_ID"] = os.getenv("STRIPE_PRICE_ID", "")

    # Init extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    # Register blueprints
    from .routes.auth import bp as auth_bp
    from .routes.dashboard import bp as dashboard_bp
    from .routes.audit import bp as audit_bp
    from .routes.billing import bp as billing_bp
    from .routes.settings import bp as settings_bp
    from .routes.dossier import bp as dossier_bp
    from .routes.admin import bp as admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(audit_bp, url_prefix="/audit")
    app.register_blueprint(billing_bp, url_prefix="/billing")
    app.register_blueprint(settings_bp)
    app.register_blueprint(dossier_bp)
    app.register_blueprint(admin_bp)

    # CLI
    from .cli import register_cli

    register_cli(app)

    # Security-ish headers (not "hiding", just good practice)
    @app.after_request
    def add_headers(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

    # Defensive error pages (avoid raw stack traces to end-users)
    @app.errorhandler(404)
    def not_found(_e):
        rid = str(uuid.uuid4())
        return render_template("error.html", code=404, request_id=rid, message="Página não encontrada."), 404

    @app.errorhandler(500)
    def internal_error(e):
        rid = str(uuid.uuid4())
        try:
            # Log full traceback to stdout/stderr for Render logs.
            tb = traceback.format_exc()
            app.logger.error("500 request_id=%s path=%s err=%s\n%s", rid, request.path, str(e), tb)
        except Exception:
            pass
        return render_template("error.html", code=500, request_id=rid, message="Erro interno. Consulte os logs do serviço."), 500

    return app


from . import models  # noqa: E402  (ensure models are imported for migrations)
