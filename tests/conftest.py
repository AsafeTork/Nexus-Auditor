import os

import pytest

from nexus import create_app, db


@pytest.fixture()
def app():
    os.environ["DATABASE_URL"] = "sqlite://"
    os.environ["SECRET_KEY"] = "test"
    a = create_app()
    a.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with a.app_context():
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()

