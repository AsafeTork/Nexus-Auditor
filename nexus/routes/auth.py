from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user

from .. import db
from ..models import Organization, User, Subscription

bp = Blueprint("auth", __name__)


@bp.get("/login")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))
    return render_template("auth/login.html")


@bp.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        flash("Credenciais inválidas.", "error")
        return redirect(url_for("auth.login"))
    login_user(user)
    return redirect(url_for("dashboard.home"))


@bp.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@bp.get("/register")
def register():
    return render_template("auth/register.html")


@bp.post("/register")
def register_post():
    org_name = (request.form.get("org_name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    if not org_name or not email or len(password) < 8:
        flash("Preencha organização, e-mail e senha (mínimo 8).", "error")
        return redirect(url_for("auth.register"))
    if User.query.filter_by(email=email).first():
        flash("E-mail já cadastrado.", "error")
        return redirect(url_for("auth.register"))

    try:
        org = Organization(name=org_name)
        db.session.add(org)
        db.session.flush()
        user = User(org_id=org.id, email=email, role="admin")
        user.set_password(password)
        db.session.add(user)
        # Default: trial enabled. You can switch to strict paid-only by setting this to inactive.
        db.session.add(Subscription(org_id=org.id, status="trialing"))
        db.session.commit()
        login_user(user)
        return redirect(url_for("dashboard.home"))
    except Exception:
        db.session.rollback()
        flash("Erro ao criar conta. Verifique configuração do banco e tente novamente.", "error")
        return redirect(url_for("auth.register"))
