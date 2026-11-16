from datetime import date

import routes.vehicles


def test_buckets_dates_and_money_tied_up():
    today = date(2026, 11, 16)
    stock = [
        {"brand": "honda", "model": "Shine", "inbound_date": "2026-11-10", "purchase_price": 70000},   # 6 d
        {"brand": "HONDA", "model": "Activa", "inbound_date": "17 Sep 2026", "purchase_price": 80000},  # 60 d
        {"brand": "TVS", "model": "Jupiter", "inbound_date": "01/08/2026", "purchase_price": 75000},    # 107 d
        {"brand": "TVS", "model": "XL", "inbound_date": "", "created_at": "2026-10-01T10:00:00"},     # 46 d, from added
        {"brand": "TVS", "model": "Junk", "inbound_date": "not a date"},                             # skipped
    ]
    r = routes.vehicles.stock_age_report(stock, today)
    assert r["total"] == 4
    assert {k: v["count"] for k, v in r["buckets"].items()} == {"0-30": 1, "31-60": 2, "61-90": 0, "90+": 1}
    assert r["buckets"]["90+"]["value"] == 75000
    assert [o["days"] for o in r["oldest"]] == [107, 60, 46, 6]
    assert r["oldest"][2]["dated_from"] == "added"
    assert {b["brand"]: b["90+"] for b in r["by_brand"]} == {"HONDA": 0, "TVS": 1}


def test_stock_age_route_is_not_swallowed_by_vehicle_id(mongo):
    from fastapi.testclient import TestClient
    import database, server
    db, run = mongo
    uid = run(db.users.insert_one({"username": "o", "name": "O", "role": "owner", "status": "active", "password": "x"})).inserted_id
    run(db.vehicles.insert_one({"brand": "HONDA", "model": "Shine", "status": "in_stock", "inbound_date": "2026-01-01"}))
    r = TestClient(server.app).get("/api/vehicles/stock-age",
                                   headers={"Authorization": f"Bearer {database.create_token({'sub': str(uid), 'role': 'owner'})}"})
    assert r.status_code == 200 and r.json()["total"] == 1
