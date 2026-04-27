from __future__ import annotations

import time

import stripe
from flask import Blueprint, current_app, redirect, request, url_for, jsonify
from flask_login import login_required, current_user

from .. import db
from ..models import Subscription

bp = Blueprint("billing", __name__)


@bp.get("/pricing")
@login_required
def pricing():
    # Minimal pricing page lives in dashboard for now; redirect.
    return redirect(url_for("dashboard.home"))


@bp.post("/create-checkout")
@login_required
def create_checkout():
    """
    Creates Stripe Checkout Session for subscription.
    """
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    price_id = current_app.config["STRIPE_PRICE_ID"]
    if not stripe.api_key or not price_id:
        return jsonify({"ok": False, "error": "Stripe não configurado (STRIPE_SECRET_KEY / STRIPE_PRICE_ID)."}), 400

    sub = Subscription.query.filter_by(org_id=current_user.org_id).first()
    if not sub:
        sub = Subscription(org_id=current_user.org_id, status="inactive")
        db.session.add(sub)
        db.session.commit()

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=request.host_url.rstrip("/") + url_for("dashboard.home") + "?billing=success",
        cancel_url=request.host_url.rstrip("/") + url_for("dashboard.home") + "?billing=cancel",
        metadata={"org_id": current_user.org_id},
    )
    return redirect(session.url, code=303)


@bp.post("/webhook")
def webhook():
    """
    Stripe webhook to update subscription status.
    """
    payload = request.data
    sig = request.headers.get("Stripe-Signature", "")
    secret = current_app.config["STRIPE_WEBHOOK_SECRET"]
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    if not secret:
        return "not configured", 400

    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig, secret=secret)
    except Exception as e:
        return str(e), 400

    et = event["type"]
    data = event["data"]["object"]

    def set_sub(org_id: str, status: str, customer: str = "", sub_id: str = "") -> None:
        sub = Subscription.query.filter_by(org_id=org_id).first()
        if not sub:
            sub = Subscription(org_id=org_id, status=status)
            db.session.add(sub)
        sub.status = status
        sub.updated_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if customer:
            sub.stripe_customer_id = customer
        if sub_id:
            sub.stripe_subscription_id = sub_id
        db.session.commit()

    # Minimal mapping
    if et in ("checkout.session.completed",):
        org_id = (data.get("metadata") or {}).get("org_id")
        if org_id:
            set_sub(org_id, "active", customer=data.get("customer", ""))

    if et in ("customer.subscription.updated", "customer.subscription.created"):
        org_id = (data.get("metadata") or {}).get("org_id")
        if org_id:
            set_sub(org_id, data.get("status", "active"), customer=data.get("customer", ""), sub_id=data.get("id", ""))

    if et in ("customer.subscription.deleted",):
        org_id = (data.get("metadata") or {}).get("org_id")
        if org_id:
            set_sub(org_id, "canceled", customer=data.get("customer", ""), sub_id=data.get("id", ""))

    return "ok", 200

