from __future__ import annotations

import time

import stripe
from flask import Blueprint, current_app, redirect, request, url_for, jsonify, render_template
from flask_login import login_required, current_user

from .. import db, csrf
from ..models import Subscription
from ..services.email import send_email

bp = Blueprint("billing", __name__)

PLANS = [
    {
        "id": "starter",
        "name": "Starter",
        "price": "$49",
        "period": "/mo",
        "description": "For stores up to $20k/month",
        "features": [
            "1 store monitored",
            "Daily automated scans",
            "Revenue impact scoring",
            "Top 3 priority fixes",
            "Email alerts",
        ],
        "cta": "Start free trial",
        "highlighted": False,
        "config_key": "STRIPE_PRICE_ID_STARTER",
    },
    {
        "id": "pro",
        "name": "Pro",
        "price": "$149",
        "period": "/mo",
        "description": "For stores up to $200k/month",
        "features": [
            "Up to 5 stores",
            "Hourly monitoring",
            "Full revenue impact engine",
            "Unlimited priority fixes",
            "Google Analytics integration",
            "Audit history & dossiers",
            "Slack & email alerts",
        ],
        "cta": "Start free trial",
        "highlighted": True,
        "config_key": "STRIPE_PRICE_ID_PRO",
    },
    {
        "id": "growth",
        "name": "Growth",
        "price": "$399",
        "period": "/mo",
        "description": "For stores above $200k/month or agencies",
        "features": [
            "Unlimited stores",
            "Real-time monitoring",
            "Adaptive learning engine",
            "Executive reporting",
            "Multi-user access",
            "Priority support",
            "Custom integrations",
        ],
        "cta": "Start free trial",
        "highlighted": False,
        "config_key": "STRIPE_PRICE_ID_GROWTH",
    },
]


@bp.get("/pricing")
@login_required
def pricing():
    sub = Subscription.query.filter_by(org_id=current_user.org_id).first()
    return render_template("billing/pricing.html", plans=PLANS, sub=sub)


@bp.post("/create-checkout")
@login_required
def create_checkout():
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    plan_id = request.form.get("plan_id", "pro")

    config_key = next(
        (p["config_key"] for p in PLANS if p["id"] == plan_id),
        "STRIPE_PRICE_ID_PRO",
    )
    price_id = current_app.config.get(config_key, "")

    if not stripe.api_key or not price_id:
        return jsonify({"ok": False, "error": "Stripe not configured. Set STRIPE_SECRET_KEY and price IDs in environment variables."}), 400

    sub = Subscription.query.filter_by(org_id=current_user.org_id).first()
    if not sub:
        sub = Subscription(org_id=current_user.org_id, status="inactive")
        db.session.add(sub)
        db.session.commit()

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=request.host_url.rstrip("/") + url_for("dashboard.home") + "?billing=success",
        cancel_url=request.host_url.rstrip("/") + url_for("billing.pricing") + "?billing=cancel",
        metadata={"org_id": current_user.org_id, "plan_id": plan_id},
    )
    return redirect(session.url, code=303)


@bp.post("/webhook")
@csrf.exempt
def webhook():
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

    if et == "checkout.session.completed":
        org_id = (data.get("metadata") or {}).get("org_id")
        customer_email = data.get("customer_details", {}).get("email", "")
        if org_id:
            set_sub(org_id, "active", customer=data.get("customer", ""))
            if customer_email:
                send_email(
                    to=customer_email,
                    subject="Welcome to Xentinel — your trial has started",
                    html=_welcome_email(customer_email),
                )

    if et in ("customer.subscription.updated", "customer.subscription.created"):
        org_id = (data.get("metadata") or {}).get("org_id")
        if org_id:
            set_sub(
                org_id,
                data.get("status", "active"),
                customer=data.get("customer", ""),
                sub_id=data.get("id", ""),
            )

    if et == "customer.subscription.deleted":
        org_id = (data.get("metadata") or {}).get("org_id")
        if org_id:
            set_sub(org_id, "canceled", customer=data.get("customer", ""), sub_id=data.get("id", ""))

    if et == "invoice.payment_failed":
        customer_email = data.get("customer_email", "")
        if customer_email:
            send_email(
                to=customer_email,
                subject="Action required: payment failed for your Xentinel subscription",
                html=_payment_failed_email(customer_email),
            )

    if et == "customer.subscription.trial_will_end":
        customer_email = (data.get("customer_details") or {}).get("email", "")
        if not customer_email:
            try:
                stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
                cust = stripe.Customer.retrieve(data.get("customer", ""))
                customer_email = cust.get("email", "")
            except Exception:
                pass
        if customer_email:
            send_email(
                to=customer_email,
                subject="Your Xentinel trial ends in 3 days",
                html=_trial_ending_email(customer_email),
            )

    return "ok", 200


def _welcome_email(email: str) -> str:
    return f"""
    <div style="font-family:Inter,sans-serif;max-width:480px;margin:0 auto;padding:32px;background:#0b0b0c;color:#f8fafc;border-radius:12px;">
      <h1 style="font-size:24px;font-weight:800;color:#fff;margin-bottom:8px;">Welcome to Xentinel</h1>
      <p style="color:#94a3b8;margin-bottom:24px;">Your 14-day free trial has started. Here's what to do next:</p>
      <ol style="color:#e2e8f0;padding-left:20px;line-height:2;">
        <li>Sign in to your dashboard</li>
        <li>Add your store URL</li>
        <li>Connect your AI provider in Settings</li>
        <li>Run your first audit and see what's at risk</li>
      </ol>
      <p style="margin-top:32px;color:#94a3b8;font-size:12px;">Questions? Just reply to this email. — The Xentinel team</p>
    </div>
    """


def _payment_failed_email(email: str) -> str:
    return f"""
    <div style="font-family:Inter,sans-serif;max-width:480px;margin:0 auto;padding:32px;background:#0b0b0c;color:#f8fafc;border-radius:12px;">
      <h1 style="font-size:24px;font-weight:800;color:#fb7185;margin-bottom:8px;">Payment failed</h1>
      <p style="color:#94a3b8;margin-bottom:24px;">We couldn't process the payment for your Xentinel subscription.</p>
      <p style="color:#e2e8f0;">Please update your payment method to keep your store protected. If we don't receive payment within 7 days, your account will be paused.</p>
      <p style="margin-top:32px;color:#94a3b8;font-size:12px;">Need help? Reply to this email. — The Xentinel team</p>
    </div>
    """


def _trial_ending_email(email: str) -> str:
    return f"""
    <div style="font-family:Inter,sans-serif;max-width:480px;margin:0 auto;padding:32px;background:#0b0b0c;color:#f8fafc;border-radius:12px;">
      <h1 style="font-size:24px;font-weight:800;color:#fff;margin-bottom:8px;">Your trial ends in 3 days</h1>
      <p style="color:#94a3b8;margin-bottom:24px;">Your Xentinel free trial is almost over. Don't lose your revenue protection.</p>
      <p style="color:#e2e8f0;">To keep monitoring your store and protecting your sales, upgrade to a paid plan before your trial ends.</p>
      <p style="margin-top:32px;color:#94a3b8;font-size:12px;">Questions about pricing? Just reply to this email. — The Xentinel team</p>
    </div>
    """
