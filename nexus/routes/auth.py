from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for, flash, session
from flask_login import login_user, logout_user, login_required, current_user

from .. import db, oauth, limiter
from ..models import Organization, User, Subscription

bp = Blueprint("auth", __name__)


@bp.get("/login")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))
    return render_template("auth/login.html")


@bp.post("/login")
@limiter.limit("12 per minute; 60 per hour")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        flash("Invalid credentials.", "error")
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
        flash("Please provide organization, email, and password (min 8 chars).", "error")
        return redirect(url_for("auth.register"))

    if User.query.filter_by(email=email).first():
        flash("Email already registered.", "error")
        return redirect(url_for("auth.register"))

    try:
        org = Organization(name=org_name)
        db.session.add(org)
        db.session.flush()
        # Default new accounts to "member" (no admin console).
        user = User(org_id=org.id, email=email, role="member")
        user.set_password(password)
        db.session.add(user)
        # Default: trial enabled
        db.session.add(Subscription(org_id=org.id, status="trialing"))
        db.session.commit()
        login_user(user)
        return redirect(url_for("dashboard.home"))
    except Exception:
        db.session.rollback()
        flash("Failed to create account. Please try again.", "error")
        return redirect(url_for("auth.register"))


@bp.get("/oauth/<provider>")
def oauth_start(provider: str):
    """
    Start OAuth login for google/github.
    If credentials are missing, show a friendly error.
    """
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))

    provider = (provider or "").strip().lower()
    if provider not in ("google", "github"):
        flash("Invalid OAuth provider.", "error")
        return redirect(url_for("auth.login"))

    client = oauth.create_client(provider)
    if not client:
        cb = url_for("auth.oauth_callback", provider=provider, _external=True)
        flash(
            f"OAuth {provider} is not configured. Set env vars OAUTH_{provider.upper()}_CLIENT_ID/SECRET "
            f"and register the callback URL: {cb}",
            "error",
        )
        return redirect(url_for("auth.login"))

    redirect_uri = url_for("auth.oauth_callback", provider=provider, _external=True)
    return client.authorize_redirect(redirect_uri)


@bp.get("/oauth/<provider>/callback")
def oauth_callback(provider: str):
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))

    provider = (provider or "").strip().lower()
    client = oauth.create_client(provider)
    if not client:
        flash("OAuth is not configured.", "error")
        return redirect(url_for("auth.login"))

    try:
        token = client.authorize_access_token()
    except Exception as e:
        flash(f"OAuth failed ({provider}): {type(e).__name__}", "error")
        return redirect(url_for("auth.login"))

    email = ""
    try:
        if provider == "google":
            # Preferred: ID token claims (fast)
            try:
                userinfo = client.parse_id_token(token) or {}
            except Exception:
                userinfo = {}
            email = (userinfo.get("email") or "").strip().lower()

            # Fallbacks: userinfo endpoints (more reliable across providers/proxies)
            if not email:
                for endpoint in (
                    "userinfo",
                    "https://openidconnect.googleapis.com/v1/userinfo",
                    "https://www.googleapis.com/oauth2/v3/userinfo",
                ):
                    try:
                        info = client.get(endpoint).json() or {}
                        email = (info.get("email") or "").strip().lower()
                        if email:
                            break
                    except Exception:
                        continue
        elif provider == "github":
            user = client.get("user").json()
            email = (user.get("email") or "").strip().lower()
            if not email:
                # get primary email
                emails = client.get("user/emails").json()
                if isinstance(emails, list):
                    for e in emails:
                        if e.get("primary") and e.get("verified"):
                            email = (e.get("email") or "").strip().lower()
                            break
                    if not email and emails:
                        email = (emails[0].get("email") or "").strip().lower()
    except Exception:
        email = ""

    if not email:
        # Avoid leaking tokens to end-users; log only high-level debug for server logs.
        try:
            import uuid

            rid = str(uuid.uuid4())
            safe_keys = sorted([str(k) for k in (token or {}).keys()]) if isinstance(token, dict) else []
            bp.logger.warning("oauth_email_missing request_id=%s provider=%s token_keys=%s", rid, provider, safe_keys)
        except Exception:
            rid = ""
        flash("Could not retrieve your email from the OAuth provider.", "error")
        return redirect(url_for("auth.login"))

    user = User.query.filter_by(email=email).first()
    if user:
        login_user(user)
        return redirect(url_for("dashboard.home"))

    # Auto-provision new org + user (trial)
    try:
        org = Organization(name=f"Org for {email}")
        db.session.add(org)
        db.session.flush()
        # Default OAuth-created users to "member" (no admin console).
        user = User(org_id=org.id, email=email, role="member")
        # random password placeholder (OAuth users won't use it normally)
        user.set_password("oauth-user-placeholder-" + email)
        db.session.add(user)
        db.session.add(Subscription(org_id=org.id, status="trialing"))
        db.session.commit()
        login_user(user)
        return redirect(url_for("dashboard.home"))
    except Exception:
        db.session.rollback()
        flash("Failed to create account via OAuth. Please try again.", "error")
