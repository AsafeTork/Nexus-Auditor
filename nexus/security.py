from __future__ import annotations

from functools import wraps

from flask import abort
from flask_login import current_user

from .models import is_org_admin


def require_admin(fn):
    """
    Simple RBAC guard.
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)
        if not is_org_admin(current_user):
            abort(403)
        return fn(*args, **kwargs)

    return wrapper


def require_master(fn):
    """
    Só o e-mail Master pode conceder/remover admin de outros usuários.
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)
        # current_user.is_admin já considera MASTER_ADMIN_EMAIL,
        # mas aqui queremos trava explícita por e-mail master.
        import os

        master = (os.getenv("MASTER_ADMIN_EMAIL", "asafetork@gmail.com") or "").strip().lower()
        if not master or (current_user.email or "").lower() != master:
            abort(403)
        return fn(*args, **kwargs)

    return wrapper
