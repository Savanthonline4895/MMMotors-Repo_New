import os
from datetime import datetime
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

from database import audit_diff, audit_ref


def test_diff_lists_only_changed_fields():
    before = {"_id": 1, "total": 100, "payment_mode": "Cash", "notes": "a", "updated_at": "x"}
    after  = {"_id": 1, "total": 90,  "payment_mode": "Cash", "notes": "a", "updated_at": "y", "discount": 10}
    assert audit_diff(before, after) == {"total": [100, 90], "discount": [None, 10]}


def test_diff_never_stores_password_hashes():
    d = audit_diff({"password": "$2b$old"}, {"password": "$2b$new"})
    assert d == {"password": ["(old)", "(new)"]}


def test_diff_skips_stock_log_and_handles_datetimes():
    t = datetime(2026, 11, 9, 10, 0)
    d = audit_diff({"stock_log": [1], "ordered_at": None}, {"stock_log": [1, 2], "ordered_at": t})
    assert d == {"ordered_at": [None, "2026-11-09 10:00:00"]}


def test_diff_of_identical_docs_is_empty():
    assert audit_diff({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2}]}) == {}


def test_ref_prefers_bill_number():
    assert audit_ref({"name": "Oil", "bill_number": "PRT-000012"}) == "PRT-000012"
    assert audit_ref({"username": "ravi"}) == "ravi"
    assert audit_ref({"booking_number": "BKG-000001", "customer_name": "x"}) == "BKG-000001"
    assert audit_ref({}) == ""


def test_edit_over_http_is_recorded_with_the_user(mongo):
    """Whole request path: auth sets the user, the middleware diffs before/after."""
    from fastapi.testclient import TestClient
    import database, server
    db, run = mongo
    uid = run(db.users.insert_one({"username": "ravi", "name": "Ravi", "role": "owner",
                                   "status": "active", "password": "x"})).inserted_id
    pid = run(db.spare_parts.insert_one({"part_number": "OIL-1", "name": "Oil", "selling_price": 100.0,
                                         "stock": 3})).inserted_id
    token = database.create_token({"sub": str(uid), "role": "owner"})
    client = TestClient(server.app)   # no `with`: skips lifespan, so no real Atlas connection
    r = client.put(f"/api/parts/{pid}", json={"selling_price": 120.0},
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    entry = run(db.audit_log.find_one({}))
    assert entry["user"] == "ravi" and entry["ref"] == "OIL-1" and entry["action"] == "edit"
    assert entry["changes"]["selling_price"] == [100.0, 120.0]
    listed = client.get("/api/audit-log", headers={"Authorization": f"Bearer {token}"}).json()
    assert [e["ref"] for e in listed] == ["OIL-1"]
