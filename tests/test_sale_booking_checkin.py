import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import models
import routes.sales
import routes.service
import routes.vehicle_bookings
from conftest import OWNER


async def _setup(db):
    cust = str((await db.customers.insert_one({"name": "Asha", "mobile": "9000000001"})).inserted_id)
    bike = str((await db.vehicles.insert_one({"brand": "HONDA", "model": "Shine", "status": "in_stock"})).inserted_id)
    bk = await routes.vehicle_bookings.create_vehicle_booking(routes.vehicle_bookings.VehicleBookingCreate(
        customer_name="Asha", customer_mobile="9000000001", brand="HONDA", model="Shine", advance_amount=5000),
        current_user=OWNER)
    return cust, bike, bk


def _sale(cust, bike, **kw):
    return models.SaleCreate(customer_id=cust, vehicle_id=bike, sale_price=90000, total_amount=90000, **kw)


# ── #5 booking → sale ────────────────────────────────────────────────────────

def test_sale_closes_booking_and_records_advance_then_delete_reopens(mongo):
    db, run = mongo
    async def go():
        cust, bike, bk = await _setup(db)
        await routes.sales.create_sale(_sale(cust, bike, booking_id=bk["id"]), current_user=OWNER)
        sale = await db.sales.find_one({})
        assert (sale["booking_number"], sale["booking_advance"], sale["total_amount"]) == ("BKG-000001", 5000, 90000)
        booking = await db.vehicle_bookings.find_one({})
        assert booking["status"] == "delivered" and booking["invoice_number"] == sale["invoice_number"]
        assert booking["sale_id"] == str(sale["_id"])

        await routes.sales.delete_sale(str(sale["_id"]), current_user=OWNER)
        booking = await db.vehicle_bookings.find_one({})
        assert booking["status"] == "arrived" and "invoice_number" not in booking and "sale_id" not in booking
    run(go())


def test_closed_booking_cannot_be_linked_and_nothing_is_saved(mongo):
    db, run = mongo
    async def go():
        cust, bike, bk = await _setup(db)
        await db.vehicle_bookings.update_one({}, {"$set": {"status": "cancelled"}})
        with pytest.raises(HTTPException) as e:
            await routes.sales.create_sale(_sale(cust, bike, booking_id=bk["id"]), current_user=OWNER)
        assert e.value.status_code == 409
        assert await db.sales.count_documents({}) == 0
        assert (await db.vehicles.find_one({}))["status"] == "in_stock"
    run(go())


def test_sale_without_booking_is_unchanged(mongo):
    db, run = mongo
    async def go():
        cust, bike, _ = await _setup(db)
        await routes.sales.create_sale(_sale(cust, bike), current_user=OWNER)
        sale = await db.sales.find_one({})
        assert "booking_id" not in sale and "booking_advance" not in sale
        assert (await db.vehicle_bookings.find_one({}))["status"] == "waiting"
    run(go())


def test_sale_pdf_shows_advance_and_balance(mongo):
    db, run = mongo
    async def go():
        cust, bike, bk = await _setup(db)
        await routes.sales.create_sale(_sale(cust, bike, booking_id=bk["id"]), current_user=OWNER)
        return await db.sales.find_one({})
    sale = run(go())
    # Render has Liberation/DejaVu; elsewhere stand in ReportLab's bundled Vera fonts
    import os, reportlab
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    vera = os.path.join(os.path.dirname(reportlab.__file__), "fonts")
    for name, f in (("Sans", "Vera.ttf"), ("Sans-Bold", "VeraBd.ttf"), ("Sans-Italic", "VeraIt.ttf"),
                    ("Mono", "Vera.ttf"), ("Mono-Bold", "VeraBd.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, os.path.join(vera, f)))
    pdf = routes.sales._generate_sale_pdf(sale)
    assert pdf[:4] == b"%PDF"


# ── #9 check-in condition ────────────────────────────────────────────────────

ID1, ID2 = "a" * 24, "b" * 24


def test_job_card_saves_checkin_photos_fuel_and_damage(mongo):
    db, run = mongo
    async def go():
        cust = str((await db.customers.insert_one({"name": "Ravi", "mobile": "9000000002"})).inserted_id)
        job = await routes.service.create_service_job(models.ServiceJobCreate(
            customer_id=cust, vehicle_number="KA07AB1234", brand="honda", model="Shine", complaint="Oil change",
            checkin_photo_ids=[ID1, ID1, ID2], fuel_level="1/2", damage_notes=" scratch on tank "), current_user=OWNER)
        saved = await db.service_jobs.find_one({})
        assert saved["checkin_photo_ids"] == [ID1, ID2]          # duplicates dropped
        assert (saved["fuel_level"], saved["damage_notes"]) == ("1/2", "scratch on tank")

        await routes.service.update_service_job(job["id"], models.ServiceJobUpdate(checkin_photo_ids=[ID2]), current_user=OWNER)
        assert (await db.service_jobs.find_one({}))["checkin_photo_ids"] == [ID2]
        with pytest.raises(HTTPException):
            await routes.service.update_service_job(job["id"], models.ServiceJobUpdate(checkin_photo_ids=["../etc"]), current_user=OWNER)
    run(go())


def test_checkin_limits_are_enforced_by_the_model():
    with pytest.raises(ValidationError):
        models.ServiceJobUpdate(fuel_level="Half")
    with pytest.raises(ValidationError):
        models.ServiceJobUpdate(checkin_photo_ids=[ID1] * 9)
    with pytest.raises(ValidationError):
        models.ServiceJobUpdate(damage_notes="x" * 501)


def test_service_staff_can_upload_checkin_photos(mongo):
    import database
    assert database.page_access_allowed("/api/upload", ["service"])
    assert database.page_access_allowed("/api/files/abc", ["service"])
    assert not database.page_access_allowed("/api/upload", ["parts"])
