from nexus import create_app, db


def test_register_and_login(client):
    r = client.post(
        "/register",
        data={"org_name": "Acme", "email": "a@a.com", "password": "password123"},
        follow_redirects=True,
    )
    assert r.status_code == 200

    client.get("/logout")
    r2 = client.post("/login", data={"email": "a@a.com", "password": "password123"}, follow_redirects=True)
    assert r2.status_code == 200


def test_home_requires_auth(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 401)

