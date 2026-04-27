from __future__ import annotations

import click

from . import db
from .models import Organization, Subscription, User


def register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """Create DB tables (dev) without migrations."""
        db.create_all()
        click.echo("DB tables created.")

    @app.cli.command("create-admin")
    @click.option("--org", required=True, help="Organization name")
    @click.option("--email", required=True)
    @click.option("--password", required=True)
    def create_admin(org: str, email: str, password: str):
        """Create an organization + admin user."""
        if User.query.filter_by(email=email.lower()).first():
            raise click.ClickException("Email already exists")
        o = Organization(name=org)
        db.session.add(o)
        db.session.flush()
        u = User(org_id=o.id, email=email.lower(), role="admin")
        u.set_password(password)
        db.session.add(u)
        db.session.add(Subscription(org_id=o.id, status="inactive"))
        db.session.commit()
        click.echo(f"Created org={o.id} admin={u.email}")

    @app.cli.command("create-user")
    @click.option("--org-id", required=True)
    @click.option("--email", required=True)
    @click.option("--password", required=True)
    @click.option("--role", default="member")
    def create_user(org_id: str, email: str, password: str, role: str):
        """Create a user inside an existing org."""
        role = role if role in ("admin", "member") else "member"
        if User.query.filter_by(email=email.lower()).first():
            raise click.ClickException("Email already exists")
        u = User(org_id=org_id, email=email.lower(), role=role)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        click.echo(f"Created user={u.email} role={u.role}")

