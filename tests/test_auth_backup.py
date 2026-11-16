from fastapi.testclient import TestClient

import database
import server


def test_login_unknown_user_and_wrong_password_both_401(mongo):
    db, run = mongo
    run(db.users.insert_one({"username": "ravi", "name": "Ravi", "role": "staff", "status": "active",
                             "password": database.pwd_ctx.hash("right-password")}))
    c = TestClient(server.app)
    for user, pw in (("nobody", "x"), ("ravi", "wrong")):   # unknown user now also runs bcrypt
        r = c.post("/api/auth/login", json={"username": user, "password": pw})
        assert r.status_code == 401 and r.json()["detail"] == "Invalid username or password"
    assert c.post("/api/auth/login", json={"username": "ravi", "password": "right-password"}).status_code == 200


def test_auto_backup_is_off_without_key_and_rejects_wrong_key(mongo, monkeypatch):
    c = TestClient(server.app)
    monkeypatch.setattr(server, "BACKUP_KEY", "")
    assert c.get("/api/backup/auto", headers={"X-Backup-Key": ""}).status_code == 404
    monkeypatch.setattr(server, "BACKUP_KEY", "k" * 64)
    assert c.get("/api/backup/auto", headers={"X-Backup-Key": "wrong"}).status_code == 401
    assert c.get("/api/backup/auto").status_code == 401
    r = c.get("/api/backup/auto", headers={"X-Backup-Key": "k" * 64})
    assert r.status_code == 200 and r.content[:2] == b"PK"   # a real ZIP


def test_client_ip_ignores_spoofed_and_edge_entries():
    from starlette.requests import Request

    def ip(headers, client=("10.0.0.9", 1)):
        scope = {"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                 "client": client}
        return server._client_ip(Request(scope))

    # Cloudflare's own header wins over anything in X-Forwarded-For
    assert ip({"CF-Connecting-IP": "49.37.1.2", "X-Forwarded-For": "6.6.6.6, 49.37.1.2, 104.22.17.40"}) == "49.37.1.2"
    # without it: the entry before the Cloudflare edge, never the spoofable first one or the shared edge
    assert ip({"X-Forwarded-For": "6.6.6.6, 49.37.1.2, 104.22.17.40"}) == "49.37.1.2"
    assert ip({"X-Forwarded-For": "49.37.1.2, 104.22.17.40"}) == "49.37.1.2"
    assert ip({"X-Forwarded-For": "49.37.1.2"}) == "49.37.1.2"
    assert ip({}) == "10.0.0.9"   # local dev, no proxy


def test_client_ip_check_is_owner_only(mongo):
    db, run = mongo
    uid = run(db.users.insert_one({"username": "staff1", "name": "S", "role": "staff", "status": "active",
                                   "password": "x"})).inserted_id
    token = database.create_token({"sub": str(uid), "role": "staff"})
    c = TestClient(server.app)
    assert c.get("/api/auth/client-ip", headers={"Authorization": f"Bearer {token}"}).status_code == 403
