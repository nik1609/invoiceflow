import base64

from fastapi.testclient import TestClient

from app import main


def _basic(pw, user="reviewer"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_no_password_set_means_open(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    assert TestClient(main.app).get("/api/runs").status_code == 200


def test_password_blocks_everything_except_health(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret-pass")
    c = TestClient(main.app)
    r = c.get("/")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert c.get("/api/runs").status_code == 401 and c.post("/api/runs", files={"file": ("a.pdf", b"x")}).status_code == 401
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/runs", headers=_basic("wrong")).status_code == 401
    assert c.get("/api/runs", headers={"Authorization": "Basic !!!"}).status_code == 401


def test_right_password_gets_in_with_any_username(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret-pass")
    c = TestClient(main.app)
    assert c.get("/api/runs", headers=_basic("s3cret-pass")).status_code == 200
    assert c.get("/", headers=_basic("s3cret-pass", user="anyone")).status_code == 200
