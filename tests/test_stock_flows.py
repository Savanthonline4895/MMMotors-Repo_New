"""Stock must move exactly once per bill line, and a failed save must change nothing.

Runs the real route handlers against the in-memory DB from conftest.py — no HTTP, no Atlas.
"""
import json

import pytest
from fastapi import HTTPException

import server
from conftest import OWNER
import models
import routes.parts
import routes.sales
import routes.service_bills


async def _part(db, part_number, stock, **kw):
    doc = {"part_number": part_number, "name": kw.pop("name", part_number), "stock": stock,
           "reorder_level": 0, "selling_price": 100.0, "gst_rate": 18, "stock_log": [], **kw}
    doc["_id"] = (await db.spare_parts.insert_one(doc)).inserted_id
    return doc


async def _stock(db, part_number):
    return (await db.spare_parts.find_one({"part_number": part_number}))["stock"]


async def _job(db):
    res = await db.service_jobs.insert_one({"job_number": "SRV-000001", "customer_name": "Ravi",
                                            "customer_mobile": "9876543210", "vehicle_number": "KA07AB1234",
                                            "brand": "HONDA", "model": "Shine", "status": "in_progress"})
    return str(res.inserted_id)


def _data(resp):
    """Handlers return either a dict or a JSONResponse."""
    return json.loads(resp.body) if hasattr(resp, "body") else resp


def _svc_line(part_number, qty, price=100.0):
    return models.BillLineItem(description=part_number, part_number=part_number, qty=qty, unit_price=price)


def _pb_line(part, qty, price=100.0):
    return models.PartsBillItem(part_id=str(part["_id"]), part_number=part["part_number"],
                                name=part["name"], qty=qty, unit_price=price)


# ── Service bills ────────────────────────────────────────────────────────────

def test_service_bill_create_deducts_and_delete_restores(mongo):
    db, run = mongo
    async def go():
        await _part(db, "OIL-1", 10)
        await _part(db, "FLT-1", 5)
        job_id = await _job(db)
        bill = await routes.service_bills.create_service_bill(models.ServiceBillCreate(
            job_id=job_id, labour_charges=300, items=[_svc_line("OIL-1", 2), _svc_line("FLT-1", 1)]),
            current_user=OWNER)
        assert (await _stock(db, "OIL-1"), await _stock(db, "FLT-1")) == (8, 4)
        log = (await db.spare_parts.find_one({"part_number": "OIL-1"}))["stock_log"]
        assert log[-1]["bill_number"] == "SRV-B-000001" and log[-1]["qty"] == -2

        await routes.service_bills.delete_service_bill(bill["id"], current_user=OWNER)
        assert (await _stock(db, "OIL-1"), await _stock(db, "FLT-1")) == (10, 5)
        assert await db.service_bills.count_documents({}) == 0
    run(go())


def test_service_bill_short_on_one_line_changes_nothing(mongo):
    db, run = mongo
    async def go():
        await _part(db, "OIL-1", 10)
        await _part(db, "FLT-1", 1)
        job_id = await _job(db)
        with pytest.raises(HTTPException) as e:
            await routes.service_bills.create_service_bill(models.ServiceBillCreate(
                job_id=job_id, items=[_svc_line("OIL-1", 2), _svc_line("FLT-1", 3)]), current_user=OWNER)
        assert e.value.status_code == 409
        assert e.value.detail["shortages"][0]["part_number"] == "FLT-1"
        assert (await _stock(db, "OIL-1"), await _stock(db, "FLT-1")) == (10, 1)
        assert await db.service_bills.count_documents({}) == 0
        assert (await db.service_jobs.find_one({}))["status"] == "in_progress"
    run(go())


def test_service_bill_same_part_on_two_lines_is_summed(mongo):
    db, run = mongo
    async def go():
        await _part(db, "OIL-1", 3)
        job_id = await _job(db)
        with pytest.raises(HTTPException):   # 2 + 2 = 4 > 3, even though each line alone fits
            await routes.service_bills.create_service_bill(models.ServiceBillCreate(
                job_id=job_id, items=[_svc_line("OIL-1", 2), _svc_line("OIL-1", 2)]), current_user=OWNER)
        assert await _stock(db, "OIL-1") == 3
    run(go())


def test_service_bill_edit_moves_only_the_difference(mongo):
    db, run = mongo
    async def go():
        await _part(db, "OIL-1", 10)
        await _part(db, "FLT-1", 5)
        job_id = await _job(db)
        bill = await routes.service_bills.create_service_bill(models.ServiceBillCreate(
            job_id=job_id, items=[_svc_line("OIL-1", 2)]), current_user=OWNER)
        await routes.service_bills.update_service_bill(bill["id"], models.ServiceBillCreate(
            job_id=job_id, items=[_svc_line("OIL-1", 5), _svc_line("FLT-1", 1)]), current_user=OWNER)
        assert (await _stock(db, "OIL-1"), await _stock(db, "FLT-1")) == (5, 4)
    run(go())


def test_service_bill_edit_that_runs_short_leaves_old_bill_and_stock(mongo):
    db, run = mongo
    async def go():
        await _part(db, "OIL-1", 4)
        job_id = await _job(db)
        bill = await routes.service_bills.create_service_bill(models.ServiceBillCreate(
            job_id=job_id, items=[_svc_line("OIL-1", 2)]), current_user=OWNER)
        with pytest.raises(HTTPException) as e:   # restore 2 → 4 on shelf, ask for 9
            await routes.service_bills.update_service_bill(bill["id"], models.ServiceBillCreate(
                job_id=job_id, items=[_svc_line("OIL-1", 9)]), current_user=OWNER)
        assert e.value.status_code == 409
        assert await _stock(db, "OIL-1") == 2
        saved = await db.service_bills.find_one({})
        assert [i["qty"] for i in saved["items"]] == [2]
    run(go())


# ── Parts bills ──────────────────────────────────────────────────────────────

def test_parts_bill_create_edit_delete_round_trip(mongo):
    db, run = mongo
    async def go():
        p = await _part(db, "PAD-1", 10)
        bill = _data(await routes.parts.create_parts_bill(models.PartsBillCreate(
            customer_name="Walk-in", items=[_pb_line(p, 3)]), current_user=OWNER))
        assert await _stock(db, "PAD-1") == 7
        await routes.parts.update_parts_bill(bill["id"], models.PartsBillUpdate(items=[_pb_line(p, 1)]),
                                       current_user=OWNER)
        assert await _stock(db, "PAD-1") == 9
        await routes.parts.delete_parts_bill(bill["id"], current_user=OWNER)
        assert await _stock(db, "PAD-1") == 10
        assert await db.parts_bills.count_documents({}) == 0
    run(go())


def test_parts_bill_short_create_saves_no_bill_and_burns_no_number(mongo):
    db, run = mongo
    async def go():
        a = await _part(db, "PAD-1", 10)
        b = await _part(db, "CBL-1", 0)
        with pytest.raises(HTTPException) as e:
            await routes.parts.create_parts_bill(models.PartsBillCreate(
                items=[_pb_line(a, 2), _pb_line(b, 1)]), current_user=OWNER)
        assert e.value.status_code == 409
        assert await _stock(db, "PAD-1") == 10
        assert await db.parts_bills.count_documents({}) == 0
        ok = _data(await routes.parts.create_parts_bill(models.PartsBillCreate(items=[_pb_line(a, 1)]), current_user=OWNER))
        assert ok["bill_number"].endswith("000001")
    run(go())


def test_parts_bill_short_edit_leaves_bill_and_stock(mongo):
    db, run = mongo
    async def go():
        p = await _part(db, "PAD-1", 5)
        bill = _data(await routes.parts.create_parts_bill(models.PartsBillCreate(items=[_pb_line(p, 2)]), current_user=OWNER))
        with pytest.raises(HTTPException):
            await routes.parts.update_parts_bill(bill["id"], models.PartsBillUpdate(items=[_pb_line(p, 8)]),
                                           current_user=OWNER)
        assert await _stock(db, "PAD-1") == 3
        assert [i["qty"] for i in (await db.parts_bills.find_one({}))["items"]] == [2]
    run(go())


def test_parts_bill_edit_with_no_items_is_rejected(mongo):
    db, run = mongo
    async def go():
        p = await _part(db, "PAD-1", 5)
        bill = _data(await routes.parts.create_parts_bill(models.PartsBillCreate(items=[_pb_line(p, 2)]), current_user=OWNER))
        with pytest.raises(HTTPException) as e:
            await routes.parts.update_parts_bill(bill["id"], models.PartsBillUpdate(items=[]), current_user=OWNER)
        assert e.value.status_code == 400
        assert await _stock(db, "PAD-1") == 3
    run(go())


# ── Vehicle sales: accessories fitted to the bike ────────────────────────────

async def _customer_and_bike(db, gstin=""):
    c = await db.customers.insert_one({"name": "Asha", "mobile": "9000000001", "gstin": gstin})
    v = await db.vehicles.insert_one({"brand": "HONDA", "model": "Shine", "status": "in_stock",
                                      "chassis_number": "CH123", "engine_number": "EN123"})
    return str(c.inserted_id), str(v.inserted_id)


def test_sale_deducts_what_is_on_shelf_and_delete_puts_it_back(mongo):
    db, run = mongo
    async def go():
        cover = await _part(db, "TANK-COV", 1)
        guard = await _part(db, "GUARD", 5)
        cust, bike = await _customer_and_bike(db)
        fittings = [models.SaleFitting(part_id=str(cover["_id"]), name="Tank cover", qty=2),
                    models.SaleFitting(part_id=str(guard["_id"]), name="Guard", qty=1)]
        sale = _data(await routes.sales.create_sale(models.SaleCreate(
            customer_id=cust, vehicle_id=bike, sale_price=90000, fittings=fittings), current_user=OWNER))
        # only 1 tank cover on the shelf: never below zero, the other one is pending
        assert (await _stock(db, "TANK-COV"), await _stock(db, "GUARD")) == (0, 4)
        saved = await db.sales.find_one({})
        assert [(p["part_number"], p["qty_short"]) for p in saved["pending_fittings"]] == [("TANK-COV", 1)]
        assert (await db.vehicles.find_one({}))["status"] == "sold"

        await routes.sales.delete_sale(sale["id"], current_user=OWNER)
        # back what was actually deducted — 1 cover, not the 2 fitted
        assert (await _stock(db, "TANK-COV"), await _stock(db, "GUARD")) == (1, 5)
        assert (await db.vehicles.find_one({}))["status"] == "in_stock"
    run(go())


# ── Customer GSTIN snapshot (GSTR-1 files these as B2B) ──────────────────────

def test_bills_copy_the_customers_gstin(mongo):
    db, run = mongo
    async def go():
        cust, bike = await _customer_and_bike(db, gstin="29ABCDE1234F1Z5")
        await routes.sales.create_sale(models.SaleCreate(customer_id=cust, vehicle_id=bike, sale_price=1),
                                 current_user=OWNER)
        assert (await db.sales.find_one({}))["customer_gstin"] == "29ABCDE1234F1Z5"

        job = await db.service_jobs.insert_one({"job_number": "SRV-000002", "customer_id": cust,
                                                "customer_name": "Asha", "status": "in_progress"})
        await routes.service_bills.create_service_bill(models.ServiceBillCreate(job_id=str(job.inserted_id), labour_charges=100),
                                         current_user=OWNER)
        assert (await db.service_bills.find_one({}))["customer_gstin"] == "29ABCDE1234F1Z5"
    run(go())


def test_service_bill_for_job_without_customer_has_blank_gstin(mongo):
    db, run = mongo
    async def go():
        job_id = await _job(db)   # imported/old jobs have no customer_id
        await routes.service_bills.create_service_bill(models.ServiceBillCreate(job_id=job_id, labour_charges=100),
                                         current_user=OWNER)
        assert (await db.service_bills.find_one({}))["customer_gstin"] == ""
    run(go())
