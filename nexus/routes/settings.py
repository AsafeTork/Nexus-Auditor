from __future__ import annotations

import time

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from .. import db
from ..models import Subscription, User, is_org_admin
from ..security import require_admin, require_master

bp = Blueprint("settings", __name__)


@bp.get("/settings")
@login_required
def settings_home():
    sub = Subscription.query.filter_by(org_id=current_user.org_id).first()
    users = User.query.filter_by(org_id=current_user.org_id).order_by(User.created_utc.desc()).all()
    import os

    master = (os.getenv("MASTER_ADMIN_EMAIL", "asafetork@gmail.com") or "").strip().lower()
    is_master = bool(master) and (current_user.email or "").lower() == master
    return render_template(
        "settings/home.html",
        sub=sub,
        users=users,
        is_admin=is_org_admin(current_user),
        is_master=is_master,
        master_email=master,
    )


@bp.post("/settings/users")
@login_required
@require_admin
def create_user():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    role = (request.form.get("role") or "member").strip()
    if role not in ("admin", "member"):
        role = "member"
    if not email or len(password) < 8:
        flash("Informe e-mail e senha (mínimo 8).", "error")
        return redirect(url_for("settings.settings_home"))
    if User.query.filter_by(email=email).first():
        flash("E-mail já existe.", "error")
        return redirect(url_for("settings.settings_home"))
    u = User(org_id=current_user.org_id, email=email, role=role)
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    flash("Usuário criado.", "ok")
    return redirect(url_for("settings.settings_home"))


@bp.post("/settings/users/<user_id>/make_admin")
@login_required
@require_master
def make_admin(user_id: str):
    u = User.query.filter_by(id=user_id, org_id=current_user.org_id).first_or_404()
    u.role = "admin"
    db.session.commit()
    flash("Usuário promovido a admin.", "ok")
    return redirect(url_for("settings.settings_home"))


@bp.post("/settings/users/<user_id>/make_member")
@login_required
@require_master
def make_member(user_id: str):
    u = User.query.filter_by(id=user_id, org_id=current_user.org_id).first_or_404()
    u.role = "member"
    db.session.commit()
    flash("Usuário removido de admin (member).", "ok")
    return redirect(url_for("settings.settings_home"))


@bp.post("/settings/subscription/reset")
@login_required
@require_admin
def reset_subscription():
    sub = Subscription.query.filter_by(org_id=current_user.org_id).first()
    if sub:
        sub.status = "inactive"
        sub.updated_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        db.session.commit()
    flash("Assinatura marcada como inactive (apenas admin).", "ok")
    return redirect(url_for("settings.settings_home"))
