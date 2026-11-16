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
