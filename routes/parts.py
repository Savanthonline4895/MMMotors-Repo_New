"""API routes: parts (split out of server.py; code moved verbatim)."""
import asyncio
import re
from datetime import timedelta
from typing import Optional
from fastapi import Depends, HTTPException, Query
from fastapi.responses import JSONResponse
import database as _db
from database import verify_token, require_admin, next_sequence, oid, oids, obj_id, paginate_params, utcnow, ist_now, calc_gst_line, calc_bill_totals, amount_in_words, norm_brand
from models import PartOrderedUpdate, PartsBillCreate, PartsBillUpdate, SparePartCreate, SparePartUpdate, StockAdjust
from routers import api_router
from routes.service_bills import _priority_low_alerts

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

# ═══════════════════════════════════════════════════════════════════════════════
#  SPARE PARTS
# ═══════════════════════════════════════════════════════════════════════════════

@api_router.get("/parts")
async def list_parts(
    category:    Optional[str]  = Query(None),
    brand:       Optional[str]  = Query(None),
    low_stock:   Optional[bool] = Query(None),
    out_of_stock:Optional[bool] = Query(None),
    search:      Optional[str]  = Query(None),
    p=Depends(paginate_params),
    current_user=Depends(verify_token),
):
    query: dict = {}
    if category: query["category"] = category
    if brand:    query["brand"]    = brand
    if low_stock:
        query["$expr"] = {"$and":[{"$gt":["$stock",0]},{"$lte":["$stock","$reorder_level"]}]}
    if out_of_stock:
        query["stock"] = 0
    if search:
        s = re.escape(search)
        query["$or"] = [
            {"name":        {"$regex": s, "$options":"i"}},
            {"part_number": {"$regex": s, "$options":"i"}},
            {"brand":       {"$regex": s, "$options":"i"}},
        ]
    docs  = await db.spare_parts.find(query).skip(p["skip"]).limit(p["limit"]).sort("name",1).to_list(p["limit"])
    total = await db.spare_parts.count_documents(query)
    return JSONResponse(content=oids(docs), headers={"X-Total-Count": str(total)})

@api_router.post("/parts", status_code=201)
async def create_part(body: SparePartCreate, current_user=Depends(verify_token)):
    doc = body.dict()
    # Stored canonical so /parts/fittings?brand= matches whatever casing the sale sends
    doc["fitting_brands"] = sorted({norm_brand(b) for b in (doc.get("fitting_brands") or []) if b.strip()})
    pn = (doc.get("part_number") or "").strip()
    if not pn:
        pn = await next_sequence("part")
    else:
        if await db.spare_parts.find_one({"part_number": pn}):
            raise HTTPException(status_code=409, detail="Part number already exists")
    doc["part_number"] = pn
    doc["created_at"]  = utcnow().isoformat()
    result = await db.spare_parts.insert_one(doc)
    doc["id"] = str(result.inserted_id); doc.pop("_id", None)
    return doc

@api_router.get("/parts/stats/summary")
async def parts_stats(current_user=Depends(verify_token)):
    pipeline_val = [{"$group": {"_id":None,"stock_value":{"$sum":{"$multiply":["$purchase_price","$stock"]}},"selling_value":{"$sum":{"$multiply":["$selling_price","$stock"]}},"total_skus":{"$sum":1}}}]
    result = await db.spare_parts.aggregate(pipeline_val).to_list(1)
    stats  = result[0] if result else {}
    low, out, priority_low = await asyncio.gather(
        db.spare_parts.count_documents({"$expr":{"$and":[{"$gt":["$stock",0]},{"$lte":["$stock","$reorder_level"]}]}}),
        db.spare_parts.count_documents({"stock": 0}),
        # Sidebar badge: priority parts needing an order, out-of-stock ones included.
        db.spare_parts.count_documents({"priority": True, "$expr": {"$lte": ["$stock", "$reorder_level"]}}),
    )
    return {"total_skus":stats.get("total_skus",0),"low_stock":low,"out_of_stock":out,"priority_low":priority_low,"stock_value":round(stats.get("stock_value",0),2),"selling_value":round(stats.get("selling_value",0),2)}

@api_router.get("/parts/low-stock")
async def low_stock_parts(
    include_out: bool = Query(False, description="Also return parts at zero stock (order list wants them)"),
    current_user=Depends(verify_token),
):
    floor = {"$gte": ["$stock", 0]} if include_out else {"$gt": ["$stock", 0]}
    docs = await db.spare_parts.aggregate([{"$match":{"$expr":{"$and":[floor,{"$lte":["$stock","$reorder_level"]}]}}}]).to_list(None)
    return oids(docs)

@api_router.get("/parts/out-of-stock")
async def out_of_stock_parts(current_user=Depends(verify_token)):
    docs = await db.spare_parts.find({"stock":0}).to_list(None)
    return oids(docs)

@api_router.get("/parts/fittings")
async def standard_fittings(
    brand: Optional[str] = Query(None, description="Vehicle brand; omit to list every standard fitting"),
    model: Optional[str] = Query(None, description="Vehicle model; drops fittings limited to other models"),
    current_user=Depends(verify_token),
):
    """The accessories fitted as standard to a bike of this brand — what the New Sale
    form pre-fills so staff remove rather than remember."""
    query: dict = {"fitting_brands": {"$exists": True, "$ne": []}}
    if brand:
        query["fitting_brands"] = norm_brand(brand)
    docs = await db.spare_parts.find(query).sort("name", 1).to_list(None)
    if model:
        docs = [d for d in docs if fits_model(d, model, brand or "")]
    return oids(docs)

def _model_key(s) -> str:
    # Spaces dropped entirely so "Activa6G" and "Activa 6G" are the same model
    return "".join(str(s or "").upper().split())

def fits_model(part: dict, model: str, brand: str = "") -> bool:
    """A part with no Compatible Models is fitted to every model of its brands;
    otherwise only to the models listed, written with or without the brand
    ("Activa 6G" or "Honda Activa 6G"; case and spaces ignored)."""
    models = {_model_key(m) for m in (part.get("compatible_with") or []) if _model_key(m)}
    return not models or bool({_model_key(model), _model_key(f"{brand} {model}")} & models)

@api_router.get("/parts/suggest-priority")
async def suggest_priority_parts(
    days:  int = Query(60, ge=1, le=365),
    limit: int = Query(15, ge=1, le=100),
    current_user=Depends(verify_token),
):
    """Parts consumed most in the last `days`, excluding ones already on the priority
    list. Reads the stock_log audit trail, so it reflects real bills, not guesses."""
    cutoff = (utcnow() - timedelta(days=days)).isoformat()
    docs = await db.spare_parts.aggregate([
        {"$match": {"priority": {"$ne": True}, "stock_log": {"$type": "array"}}},
        {"$unwind": "$stock_log"},
        {"$match": {"stock_log.action": "subtract", "stock_log.date": {"$gte": cutoff}}},
        {"$group": {
            "_id":           "$_id",
            "part_number":   {"$first": "$part_number"},
            "name":          {"$first": "$name"},
            "category":      {"$first": "$category"},
            "stock":         {"$first": "$stock"},
            "reorder_level": {"$first": "$reorder_level"},
            "used":          {"$sum": {"$abs": "$stock_log.qty"}},
            "times":         {"$sum": 1},
        }},
        {"$sort": {"used": -1, "times": -1}},
        {"$limit": limit},
    ]).to_list(limit)
    return {"days": days, "parts": oids(docs)}

@api_router.post("/parts/{part_id}/mark-ordered")
async def mark_part_ordered(part_id: str, body: PartOrderedUpdate, current_user=Depends(verify_token)):
    """Record that a part has been ordered from a vendor. Deliberately does NOT touch
    reorder_level — an earlier version set it to 0, which hid the part from every future
    low-stock check. The part stays on the list, flagged with the date."""
    if body.ordered:
        update = {"$set": {"ordered_at": utcnow().isoformat(), "ordered_by": current_user.get("name", "")}}
    else:
        update = {"$unset": {"ordered_at": "", "ordered_by": ""}}
    res = await db.spare_parts.update_one({"_id": obj_id(part_id)}, update)
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Part not found")
    return oid(await db.spare_parts.find_one({"_id": obj_id(part_id)}))

@api_router.get("/parts/{part_id}")
async def get_part(part_id: str, current_user=Depends(verify_token)):
    doc = await db.spare_parts.find_one({"_id": obj_id(part_id)})
    if not doc:
        doc = await db.spare_parts.find_one({"part_number": part_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Part not found")
    return oid(doc)

@api_router.put("/parts/{part_id}")
async def update_part(part_id: str, body: SparePartUpdate, current_user=Depends(verify_token)):
    update = {k: v for k, v in body.dict().items() if v is not None}
    if "fitting_brands" in update:
        update["fitting_brands"] = sorted({norm_brand(b) for b in update["fitting_brands"] if b.strip()})
    await db.spare_parts.update_one({"_id": obj_id(part_id)}, {"$set": update})
    return oid(await db.spare_parts.find_one({"_id": obj_id(part_id)}))

# PATCH 5: adjust-stock by part_number (for service bill modal) ───────────────
@api_router.post("/parts/{part_number}/adjust-stock-by-number")
async def adjust_stock_by_number(
    part_number: str,
    body: StockAdjust,
    current_user=Depends(verify_token),
):
    """Adjust stock by part_number string — used by service bill modal for deduction/restore."""
    part = await db.spare_parts.find_one({"part_number": part_number})
    if not part:
        raise HTTPException(status_code=404, detail=f"Part '{part_number}' not found")
    current_stock = part.get("stock") or 0
    action = body.action or "add"
    if action == "subtract":
        new_stock = max(0, current_stock - abs(body.qty))
    else:
        new_stock = current_stock + abs(body.qty)
    update = {"$set": {"stock": new_stock}, "$push": {"stock_log": {
        "qty": body.qty, "action": action, "reason": body.reason or "service_bill",
        "new_stock": new_stock, "date": utcnow().isoformat(),
    }}}
    if action != "subtract":
        update["$unset"] = {"ordered_at": "", "ordered_by": ""}   # stock came in
    await db.spare_parts.update_one({"part_number": part_number}, update)
    return JSONResponse(content={"part_number": part_number, "old_stock": current_stock, "new_stock": new_stock})

@api_router.post("/parts/{part_id}/adjust-stock")
async def adjust_stock(part_id: str, body: StockAdjust, current_user=Depends(verify_token)):
    part = await db.spare_parts.find_one({"_id": obj_id(part_id)})
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    # PATCH 4: respect action field (add/subtract), fall back to qty sign
    action = body.action or "add"
    if action == "subtract":
        new_stock = max(0, part["stock"] - abs(body.qty))
    else:
        new_stock = max(0, part["stock"] + body.qty)
    update = {"$set": {"stock": new_stock}, "$push": {"stock_log": {
        "qty": body.qty, "action": action, "reason": body.reason,
        "new_stock": new_stock, "date": utcnow().isoformat()
    }}}
    if action != "subtract":
        update["$unset"] = {"ordered_at": "", "ordered_by": ""}   # stock came in
    await db.spare_parts.update_one({"_id": obj_id(part_id)}, update)
    return {"part_id": part_id, "new_stock": new_stock}

@api_router.delete("/parts/{part_id}")
async def delete_part(part_id: str, current_user=Depends(require_admin)):
    result = await db.spare_parts.delete_one({"_id": obj_id(part_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Part not found")
    return {"message": "Deleted"}


# ═══════════════════════════════════════════════════════════════════════════════
#  PARTS BILLS — PATCH 6 (walk-in counter, vehicle number optional)
# ═══════════════════════════════════════════════════════════════════════════════

@api_router.get("/parts-bills")
async def list_parts_bills(
    search: Optional[str] = Query(None),
    p=Depends(paginate_params),
    current_user=Depends(verify_token),
):
    query: dict = {}
    if search:
        s = re.escape(search)
        query["$or"] = [
            {"bill_number":      {"$regex": s, "$options":"i"}},
            {"customer_name":    {"$regex": s, "$options":"i"}},
            {"customer_mobile":  {"$regex": s, "$options":"i"}},
            {"customer_vehicle": {"$regex": s, "$options":"i"}},
        ]
    docs  = await db.parts_bills.find(query).skip(p["skip"]).limit(p["limit"]).sort("created_at",-1).to_list(p["limit"])
    total = await db.parts_bills.count_documents(query)
    return JSONResponse(content=oids(docs), headers={"X-Total-Count": str(total)})


@api_router.post("/parts-bills", status_code=201)
async def create_parts_bill(body: PartsBillCreate, current_user=Depends(verify_token)):
    if not body.items:
        raise HTTPException(status_code=400, detail="At least one item required")

    # Transactional: stock deducts + counter bump + bill insert are all-or-nothing,
    # so a 409 on a later line no longer leaves earlier lines deducted.
    inserted_id = None
    touched: dict = {}
    async with await _db.client.start_session() as session:
        async def _txn(s):
            nonlocal inserted_id
            touched.clear()   # a retried transaction must not double-count
            bill_no = await next_sequence("part_bill", session=s)
            items_out = []
            for item in body.items:
                # Resolve part by part_id first, fall back to part_number
                part = None
                if item.part_id:
                    part = await db.spare_parts.find_one({"_id": obj_id(item.part_id)}, session=s)
                if not part and item.part_number:
                    part = await db.spare_parts.find_one({"part_number": item.part_number}, session=s)

                if part:
                    # Atomic guarded decrement — no TOCTOU race; log entry via $push in same op
                    dec_res = await db.spare_parts.update_one(
                        {"_id": part["_id"], "stock": {"$gte": item.qty}},
                        {"$inc": {"stock": -item.qty}, "$push": {"stock_log": {
                            "qty": -item.qty, "action": "subtract",
                            "reason": "complimentary" if item.complimentary else "parts_bill",
                            "bill_number": bill_no,
                            "date": utcnow().isoformat(),
                        }}},
                        session=s,
                    )
                    if dec_res.matched_count == 0:
                        fresh = await db.spare_parts.find_one({"_id": part["_id"]}, {"stock": 1}, session=s)
                        have = (fresh or {}).get("stock", 0)
                        raise HTTPException(
                            status_code=409,
                            detail=f"Insufficient stock for {item.name} (have {have}, need {item.qty})"
                        )
                    touched[part["_id"]] = touched.get(part["_id"], 0) + item.qty

                if item.complimentary:
                    line = {"gross":0.0,"discount":0.0,"taxable":0.0,"cgst":0.0,"sgst":0.0,"gst_total":0.0,"total":0.0}
                    item_disc = 0.0
                else:
                    item_disc = max(0.0, float(item.discount or 0))
                    line = calc_gst_line(item.unit_price, item.qty, item.gst_rate, item_disc)
                items_out.append({
                    "part_id":       str(part["_id"]) if part else (item.part_id or ""),
                    "part_number":   item.part_number or (part.get("part_number","") if part else ""),
                    "name":          item.name,
                    "hsn_code":      item.hsn_code or (part.get("hsn_code","8714") if part else "8714"),
                    "qty":           item.qty,
                    "unit_price":    0 if item.complimentary else item.unit_price,
                    "mrp":           item.unit_price,
                    "gst_rate":      0 if item.complimentary else item.gst_rate,
                    "discount":      0 if item.complimentary else item_disc,
                    "complimentary": bool(item.complimentary),
                    **line,
                })

            totals  = calc_bill_totals([{"unit_price":i["unit_price"],"qty":i["qty"],"gst_rate":i["gst_rate"],"discount":i.get("discount",0)} for i in items_out])
            # Bill-level discount off grand_total (post-GST). Clamped 0..grand_total.
            bill_discount = max(0.0, min(float(body.discount or 0), totals["grand_total"]))
            net_total = round(totals["grand_total"] - bill_discount, 2)

            doc = {
                "bill_number":      bill_no,
                "customer_name":    body.customer_name or "",
                "customer_mobile":  body.customer_mobile or "",
                "customer_vehicle": body.customer_vehicle or "",
                "payment_mode":     body.payment_mode or "Cash",
                "items":            items_out,
                "discount":         bill_discount,
                "net_total":        net_total,
                "amount_in_words":  amount_in_words(net_total),
                "sold_by":          current_user.get("name",""),
                "bill_date":        ist_now().strftime("%d %b %Y"),
                "created_at":       utcnow().isoformat(),
                **totals,
            }
            res = await db.parts_bills.insert_one(doc, session=s)
            inserted_id = res.inserted_id
        await session.with_transaction(_txn)

    created = await db.parts_bills.find_one({"_id": inserted_id})
    payload = oid(created)
    payload["low_stock_alerts"] = await _priority_low_alerts(touched)
    return JSONResponse(content=payload, status_code=201)


@api_router.get("/parts-bills/{bill_id}")
async def get_parts_bill(bill_id: str, current_user=Depends(verify_token)):
    doc = await db.parts_bills.find_one({"_id": obj_id(bill_id)})
    if not doc:
        doc = await db.parts_bills.find_one({"bill_number": bill_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Parts bill not found")
    return oid(doc)


@api_router.put("/parts-bills/{bill_id}")
async def update_parts_bill(bill_id: str, body: PartsBillUpdate, current_user=Depends(verify_token)):
    """Update parts bill. If items changed, restore old stock then re-deduct new stock.
    Discount recomputed per-line + bill-level. Complimentary rows ignore discount."""
    # An empty item list would restore all stock and zero the bill — a delete, which is owner-only.
    if body.items is not None and not body.items:
        raise HTTPException(status_code=400, detail="At least one item required")
    bill = await db.parts_bills.find_one({"_id": obj_id(bill_id)})
    if not bill:
        raise HTTPException(status_code=404, detail="Parts bill not found")

    update: dict = {"updated_at": utcnow().isoformat()}
    if body.customer_name    is not None: update["customer_name"]    = body.customer_name
    if body.customer_mobile  is not None: update["customer_mobile"]  = body.customer_mobile
    if body.customer_vehicle is not None: update["customer_vehicle"] = body.customer_vehicle
    if body.payment_mode     is not None: update["payment_mode"]     = body.payment_mode

    # Transactional: restore old stock + deduct new stock + save bill are all-or-nothing,
    # so a 409 on a new line rolls the restore back instead of double-counting stock.
    touched: dict = {}
    async with await _db.client.start_session() as session:
        async def _txn(s):
            touched.clear()   # a retried transaction must not double-count
            old_qty: dict = {}
            upd = dict(update)
            if body.items is not None:
                # Restore stock from old items first
                for old_it in bill.get("items", []):
                    query = {}
                    if old_it.get("part_id"):
                        try:
                            from bson import ObjectId as _OID
                            query = {"_id": _OID(old_it["part_id"])}
                        except Exception:
                            query = {"part_number": old_it.get("part_number","")}
                    elif old_it.get("part_number"):
                        query = {"part_number": old_it["part_number"]}
                    if query and old_it.get("qty", 0) > 0:
                        # find_one_and_update, not update_one: returns the part so the net
                        # change per part can be worked out without another read.
                        restored = await db.spare_parts.find_one_and_update(query, {"$inc": {"stock": old_it["qty"]}, "$push": {"stock_log": {
                            "qty": old_it["qty"], "action": "add",
                            "reason": "parts_bill_edit_restore", "bill_number": bill.get("bill_number", ""),
                            "date": utcnow().isoformat(),
                        }}}, projection={"_id": 1}, session=s)
                        if restored:
                            old_qty[restored["_id"]] = old_qty.get(restored["_id"], 0) + old_it["qty"]

                # Build new items + deduct stock
                items_out = []
                for item in body.items:
                    part = None
                    if item.part_id:
                        part = await db.spare_parts.find_one({"_id": obj_id(item.part_id)}, session=s)
                    if not part and item.part_number:
                        part = await db.spare_parts.find_one({"part_number": item.part_number}, session=s)
                    if part:
                        # Atomic guarded decrement — no TOCTOU race
                        dec_res = await db.spare_parts.update_one(
                            {"_id": part["_id"], "stock": {"$gte": item.qty}},
                            {"$inc": {"stock": -item.qty}, "$push": {"stock_log": {
                                "qty": -item.qty, "action": "subtract",
                                "reason": "complimentary" if item.complimentary else "parts_bill_edit",
                                "bill_number": bill.get("bill_number", ""),
                                "date": utcnow().isoformat(),
                            }}},
                            session=s,
                        )
                        if dec_res.matched_count == 0:
                            fresh = await db.spare_parts.find_one({"_id": part["_id"]}, {"stock": 1}, session=s)
                            have = (fresh or {}).get("stock", 0)
                            raise HTTPException(
                                status_code=409,
                                detail=f"Insufficient stock for {item.name} (have {have}, need {item.qty})"
                            )
                        touched[part["_id"]] = touched.get(part["_id"], 0) + item.qty

                    if item.complimentary:
                        line = {"gross":0.0,"discount":0.0,"taxable":0.0,"cgst":0.0,"sgst":0.0,"gst_total":0.0,"total":0.0}
                        item_disc = 0.0
                    else:
                        item_disc = max(0.0, float(item.discount or 0))
                        line = calc_gst_line(item.unit_price, item.qty, item.gst_rate, item_disc)
                    items_out.append({
                        "part_id":       str(part["_id"]) if part else (item.part_id or ""),
                        "part_number":   item.part_number or (part.get("part_number","") if part else ""),
                        "name":          item.name,
                        "hsn_code":      item.hsn_code or (part.get("hsn_code","8714") if part else "8714"),
                        "qty":           item.qty,
                        "unit_price":    0 if item.complimentary else item.unit_price,
                        "mrp":           item.unit_price,
                        "gst_rate":      0 if item.complimentary else item.gst_rate,
                        "discount":      0 if item.complimentary else item_disc,
                        "complimentary": bool(item.complimentary),
                        **line,
                    })
                totals = calc_bill_totals([{"unit_price":i["unit_price"],"qty":i["qty"],"gst_rate":i["gst_rate"],"discount":i.get("discount",0)} for i in items_out])
                upd["items"] = items_out
                upd.update(totals)

            # Bill-level discount
            disc_raw = body.discount if body.discount is not None else bill.get("discount", 0)
            grand = upd.get("grand_total", bill.get("grand_total", 0))
            bill_discount = max(0.0, min(float(disc_raw or 0), float(grand)))
            net_total = round(float(grand) - bill_discount, 2)
            upd["discount"]        = bill_discount
            upd["net_total"]       = net_total
            upd["amount_in_words"] = amount_in_words(net_total)

            # Net removal per part across the edit — only a net decrease can cross the level
            for pid, qty in list(touched.items()):
                net = qty - old_qty.get(pid, 0)
                if net > 0:
                    touched[pid] = net
                else:
                    del touched[pid]
            await db.parts_bills.update_one({"_id": obj_id(bill_id)}, {"$set": upd}, session=s)
        await session.with_transaction(_txn)

    updated = await db.parts_bills.find_one({"_id": obj_id(bill_id)})
    payload = oid(updated)
    payload["low_stock_alerts"] = await _priority_low_alerts(touched)
    return JSONResponse(content=payload)


@api_router.delete("/parts-bills/{bill_id}")
async def delete_parts_bill(bill_id: str, current_user=Depends(require_admin)):
    bill = await db.parts_bills.find_one({"_id": obj_id(bill_id)})
    if not bill:
        raise HTTPException(status_code=404, detail="Parts bill not found")
    # Restore stock for each item
    for item in bill.get("items", []):
        query = {}
        if item.get("part_id"):
            try:
                from bson import ObjectId as _OID
                query = {"_id": _OID(item["part_id"])}
            except Exception:
                query = {"part_number": item.get("part_number","")}
        elif item.get("part_number"):
            query = {"part_number": item["part_number"]}
        if query:
            await db.spare_parts.update_one(query, {"$inc": {"stock": item["qty"]}})
    await db.parts_bills.delete_one({"_id": obj_id(bill_id)})
    return {"message": "Deleted — stock restored"}
