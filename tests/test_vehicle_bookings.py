import io
import zipfile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import database
import server
from conftest import OWNER


def _new(**kw):
    data = dict(customer_name="Ravi Kumar", customer_mobile="9876543210", brand="honda",
                model="Shine 100", color="Black", advance_amount=5000)
    data.update(kw)
    return server.VehicleBookingCreate(**data)


def test_matching_is_by_brand_model_and_colour_only_when_asked():
    stock = [{"brand": "HONDA", "model": "Shine  100", "color": "black"},
             {"brand": "HONDA", "model": "Shine 100", "color": "Red"},
             {"brand": "TVS", "model": "Shine 100", "color": "Black"}]
    assert server.booking_matches({"brand": "Honda", "model": "shine 100", "color": "Black"}, stock) == 1
    assert server.booking_matches({"brand": "HONDA", "model": "Shine 100", "color": ""}, stock) == 2
    assert server.booking_matches({"brand": "HONDA", "model": "Activa", "color": ""}, stock) == 0


def test_booking_lifecycle_numbering_matches_and_dashboard(mongo):
    db, run = mongo
    async def go():
        a = await server.create_vehicle_booking(_new(), current_user=OWNER)
        b = await server.create_vehicle_booking(_new(model="Activa"), current_user=OWNER)
        assert (a["booking_number"], b["booking_number"]) == ("BKG-000001", "BKG-000002")
        assert a["brand"] == "HONDA" and a["status"] == "waiting"

        await db.vehicles.insert_one({"brand": "HONDA", "model": "Shine 100", "color": "Black", "status": "in_stock"})
        listed = {d["booking_number"]: d["in_stock_matches"] for d in await server.list_vehicle_bookings(status=None, search=None, current_user=OWNER)}
        assert listed == {"BKG-000001": 1, "BKG-000002": 0}
        assert await server._bookings_ready() == 1

        with pytest.raises(HTTPException) as e:   # delivered needs the sale invoice
            await server.update_vehicle_booking(a["id"], server.VehicleBookingUpdate(status="delivered"), current_user=OWNER)
        assert e.value.status_code == 400
        done = await server.update_vehicle_booking(
            a["id"], server.VehicleBookingUpdate(status="delivered", invoice_number="INV-000123"), current_user=OWNER)
        assert done["status"] == "delivered" and "delivered_at" in done
        assert await server._bookings_ready() == 0

        with pytest.raises(HTTPException):        # refund can't exceed the advance
            await server.update_vehicle_booking(
                b["id"], server.VehicleBookingUpdate(status="cancelled", refund_amount=9000), current_user=OWNER)
        with pytest.raises(HTTPException):
            await server.update_vehicle_booking(b["id"], server.VehicleBookingUpdate(status="lost"), current_user=OWNER)
    run(go())


def test_bad_mobile_and_negative_advance_are_rejected():
    with pytest.raises(Exception):
        _new(customer_mobile="12345")
    with pytest.raises(Exception):
        _new(advance_amount=-1)


def test_staff_access_and_owner_only_delete(mongo):
    db, run = mongo
    uid = run(db.users.insert_one({"username": "s1", "name": "S", "role": "sales", "status": "active",
                                   "password": "x", "allowed_pages": ["bookings"]})).inserted_id
    staff = {"Authorization": f"Bearer {database.create_token({'sub': str(uid), 'role': 'sales'})}"}
    c = TestClient(server.app)
    r = c.post("/api/vehicle-bookings", headers=staff, json={"customer_name": "A", "customer_mobile": "9000000000",
                                                            "brand": "TVS", "model": "Jupiter", "advance_amount": 2000})
    assert r.status_code == 201, r.text
    assert c.get("/api/vehicle-bookings", headers=staff).status_code == 200
    assert c.delete(f"/api/vehicle-bookings/{r.json()['id']}", headers=staff).status_code == 403


def test_backup_zip_includes_bills_and_bookings(mongo):
    db, run = mongo
    run(server.create_vehicle_booking(_new(), current_user=OWNER))
    run(db.service_bills.insert_one({"bill_number": "SRV-B-000001", "items": [{"description": "Oil"}]}))
    data, _ = run(server._build_backup_zip())
    names = " ".join(zipfile.ZipFile(io.BytesIO(data)).namelist())
    for part in ("service_bills_", "parts_bills_", "purchase_bills_", "vendors_", "vehicle_bookings_"):
        assert part in names, part
