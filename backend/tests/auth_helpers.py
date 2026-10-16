"""Phase 2A test helpers: an app with the real auth guard, and a TestClient
signed in as a real user with a real membership (no auth bypass)."""
import uuid

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import auth
import db
from api import auth as auth_api

PASSWORD = "correct horse battery staple"


def make_app(*routers) -> FastAPI:
    app = FastAPI(dependencies=[Depends(auth.guard)])
    app.include_router(auth_api.router)
    for r in routers:
        app.include_router(r)
    return app


def make_user(conn, org: str | None, role: str = "OWNER", email: str | None = None,
              password: str = PASSWORD) -> dict:
    email = email or f"user-{uuid.uuid4().hex[:10]}@example.test"
    user = db.create_user(conn, email, f"Test {role.title()}", auth.hash_password(password))
    if org:
        db.add_membership(conn, org, str(user["id"]), role)
    conn.commit()
    return user


def signed_in(app_or_routers, conn, org: str, role: str = "OWNER", user: dict | None = None) -> TestClient:
    """A TestClient signed in to `org` as a member with `role`. CSRF header set."""
    app = app_or_routers if isinstance(app_or_routers, FastAPI) else make_app(*app_or_routers)
    user = user or make_user(conn, org, role)
    c = TestClient(app)
    r = c.post("/auth/login", json={"email": user["email"], "password": PASSWORD, "organization_id": org})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    c.user = user
    c.org = org
    return c
