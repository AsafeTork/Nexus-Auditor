from __future__ import annotations

import time
import uuid
import os
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, login_manager


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Organization(db.Model):
    __tablename__ = "orgs"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(200), nullable=False)
    created_utc = db.Column(db.String(40), default=utc_now)
    # Optional per-org LLM defaults (override env vars in UI/worker)
    llm_base_url_v1 = db.Column(db.String(1000), default="")
    llm_model = db.Column(db.String(200), default="")


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id = db.Column(db.String(36), db.ForeignKey("orgs.id"), nullable=False)
    email = db.Column(db.String(320), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(32), default="member")  # admin|member
    created_utc = db.Column(db.String(40), default=utc_now)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self) -> bool:
        """
        Compatibilidade com a regra "current_user.is_admin".
        Admin = role=admin OU e-mail Master (via env MASTER_ADMIN_EMAIL).
        """
        return is_org_admin(self)


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, user_id)


class Site(db.Model):
    __tablename__ = "sites"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id = db.Column(db.String(36), db.ForeignKey("orgs.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    base_url = db.Column(db.String(1000), nullable=False)
    created_utc = db.Column(db.String(40), default=utc_now)


class AuditRun(db.Model):
    __tablename__ = "audit_runs"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id = db.Column(db.String(36), db.ForeignKey("orgs.id"), nullable=False, index=True)
    site_id = db.Column(db.String(36), db.ForeignKey("sites.id"), nullable=False, index=True)
    created_utc = db.Column(db.String(40), default=utc_now)
    updated_utc = db.Column(db.String(40), default=utc_now)
    status = db.Column(db.String(32), default="queued")  # queued|running|done|error

    model = db.Column(db.String(200), nullable=False)
    provider_base_url_v1 = db.Column(db.String(1000), nullable=False)

    # Outputs
    logs = db.Column(db.Text, default="")
    markdown_text = db.Column(db.Text, default="")
    csv_text = db.Column(db.Text, default="")

    # Download metadata
    target_domain = db.Column(db.String(255), default="")


class AuditEvent(db.Model):
    __tablename__ = "audit_events"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    audit_run_id = db.Column(db.String(36), db.ForeignKey("audit_runs.id"), nullable=False, index=True)
    # epoch milliseconds exceed 32-bit int; use BigInteger
    ts_ms = db.Column(db.BigInteger, default=lambda: int(time.time() * 1000))
    layer = db.Column(db.String(200), default="system")
    level = db.Column(db.String(16), default="INFO")
    message = db.Column(db.Text, default="")


class Subscription(db.Model):
    __tablename__ = "subscriptions"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id = db.Column(db.String(36), db.ForeignKey("orgs.id"), nullable=False, index=True)
    status = db.Column(db.String(32), default="trialing")  # inactive|trialing|active|past_due|canceled
    stripe_customer_id = db.Column(db.String(128), default="")
    stripe_subscription_id = db.Column(db.String(128), default="")
    created_utc = db.Column(db.String(40), default=utc_now)
    updated_utc = db.Column(db.String(40), default=utc_now)


def is_org_admin(user: User) -> bool:
    if not user:
        return False

    master = (os.getenv("MASTER_ADMIN_EMAIL", "asafetork@gmail.com") or "").strip().lower()
    if master and str(getattr(user, "email", "") or "").lower() == master:
        return True

    # Backward/forward compatible admin check.
    role = str(getattr(user, "role", "") or "").lower()
    if role == "admin":
        return True

    # Some older databases may still have is_admin boolean.
    try:
        return bool(getattr(user, "is_admin", False))
    except Exception:
        return False


def is_subscription_active(sub: Subscription | None) -> bool:
    if not sub:
        return False
    return str(sub.status or "").lower() in ("trialing", "active")
