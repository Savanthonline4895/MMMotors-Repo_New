"""API routes: service bills (split out of server.py; code moved verbatim)."""
from typing import Optional
from fastapi import Depends, HTTPException, Query
from fastapi.responses import JSONResponse
import database as _db
from database import verify_token, require_admin, oid, oids, obj_id, paginate_params, utcnow, ist_now, calc_gst_line, calc_bill_totals, amount_in_words
from models import ServiceBillCreate
from routers import api_router

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

# ═══════════════════════════════════════════════════════════════════════════════
#  SERVICE BILLS (GST)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Stock resolution + shortage aggregation (used by create/update service_bill) ──
#
# Service bill items don't carry part_id (unlike parts_bills), so we resolve via
# 3-tier match: part_number → aliases.part_number → name (unique). Ambiguous name
# matches (>1 hit) skip silently — safer than deducting the wrong part.
async def _resolve_spare_part(desc: str, part_number: str, session=None):
    pn = (part_number or "").strip()
    if pn:
        part = await db.spare_parts.find_one({"part_number": pn}, session=session)
        if part:
            return part
        part = await db.spare_parts.find_one({"aliases.part_number": pn}, session=session)
        if part:
            return part
    name = (desc or "").strip()
    if name:
        matches = await db.spare_parts.find({"name": name}, session=session).to_list(2)
        if len(matches) == 1:
            return matches[0]
    return None

async def _aggregate_bill_needs(items, session=None):
    """Aggregate items → {part_id_str: {"part": doc, "qty": total}}.
    Multiple lines pointing at the same part are summed. Skips items with no
    match or qty <= 0."""
    needs: dict = {}
    for it in items or []:
        qty = int(getattr(it, "qty", 0) or 0)
        if qty <= 0:
            continue
        desc = getattr(it, "description", "") or ""
        pn   = getattr(it, "part_number", "") or ""
        part = await _resolve_spare_part(desc, pn, session=session)
        if not part:
            continue
        key = str(part["_id"])
        if key in needs:
            needs[key]["qty"] += qty
        else:
            needs[key] = {"part": part, "qty": qty}
    return needs

async def _deduct_fittings(fittings, invoice_no: str, session=None, reason: str = "sale_fitting"):
    """Take accessory stock off for the fittings on a sale.

    A vehicle sale is never blocked by a missing seat cover, so this deducts what is
    actually on the shelf and reports the rest instead of raising. Stock is never driven
    below zero. Returns (items_out, touched, pending):
      items_out — the lines to store on the sale, each carrying `qty_deducted` so an
                  edit or delete can put back exactly what was taken
      touched   — {part _id: qty deducted}, feeds the priority low-stock alert
      pending   — what could not be deducted; drives the warning marker on the sales row
    """
    items_out, touched, pending = [], {}, []
    for f in fittings or []:
        qty = int(getattr(f, "qty", 0) or 0)
        if qty <= 0:
            continue
        name = getattr(f, "name", "") or ""
        pid  = getattr(f, "part_id", "") or ""
        pn   = getattr(f, "part_number", "") or ""
        free = bool(getattr(f, "complimentary", False))
        part = None
        if pid:
            try:
                part = await db.spare_parts.find_one({"_id": obj_id(pid)}, session=session)
            except HTTPException:
                part = None
        if not part and pn:
            part = await db.spare_parts.find_one({"part_number": pn}, session=session)

        take = 0
        if part:
            take = min(int(part.get("stock", 0) or 0), qty)
            if take > 0:
                await db.spare_parts.update_one(
                    {"_id": part["_id"]},
                    {"$inc": {"stock": -take}, "$push": {"stock_log": {
                        "qty": -take, "action": "subtract",
                        "reason": "complimentary" if free else reason,
                        "bill_number": invoice_no, "date": utcnow().isoformat(),
                    }}},
                    session=session,
                )
                touched[part["_id"]] = touched.get(part["_id"], 0) + take
        if take < qty:
            # Not in the parts list at all, or not enough on the shelf. Either way the
            # bike still goes out; the shortfall becomes the pending-fitting marker.
            pending.append({
                "part_number": (part or {}).get("part_number", "") or pn,
                "name":        (part or {}).get("name", "") or name,
                "qty_short":   qty - take,
                "untracked":   part is None,
            })
        items_out.append({
            "part_id":       str(part["_id"]) if part else (pid or ""),
            "part_number":   (part or {}).get("part_number", "") or pn,
            "name":          name or (part or {}).get("name", ""),
            "qty":           qty,
            "qty_deducted":  take,
            "unit_price":    0.0 if free else round(float(getattr(f, "unit_price", 0) or 0), 2),
            "mrp":           round(float(getattr(f, "unit_price", 0) or 0), 2),
            "complimentary": free,
        })
    return items_out, touched, pending

async def _restore_fittings(fittings, invoice_no: str, session=None, reason: str = "sale_fitting_restore"):
    """Put fitting stock back — only what was actually deducted, which is why pending
    shortfalls are stored on the sale rather than recalculated here."""
    for f in fittings or []:
        qty = int((f or {}).get("qty_deducted", 0) or 0)
        if qty <= 0:
            continue
        query = None
        if f.get("part_id"):
            try:
                query = {"_id": obj_id(f["part_id"])}
            except HTTPException:
                query = None
        if query is None and f.get("part_number"):
            query = {"part_number": f["part_number"]}
        if not query:
            continue
        await db.spare_parts.update_one(
            query,
            {"$inc": {"stock": qty}, "$push": {"stock_log": {
                "qty": qty, "action": "add", "reason": reason,
                "bill_number": invoice_no, "date": utcnow().isoformat(),
            }}},
            session=session,
        )

async def _priority_low_alerts(touched: dict):
    """touched: {part ObjectId: qty just deducted}. Returns the priority parts this bill
    just pushed into needing an order:
      - it crossed the reorder level (above it before, at or below now), or
      - it just hit zero, which is worth saying even if it was already low.
    Crossings only, so a part sitting below its level does not nag on every later bill.
    Called after the transaction commits; a failed bill deducted nothing."""
    if not touched:
        return []
    docs = await db.spare_parts.find(
        {"_id": {"$in": list(touched)}, "priority": True},
        {"part_number": 1, "name": 1, "stock": 1, "reorder_level": 1},
    ).to_list(None)
    alerts = []
    for d in docs:
        after = int(d.get("stock", 0) or 0)
        level = int(d.get("reorder_level", 0) or 0)
        before = after + int(touched.get(d["_id"], 0) or 0)
        if (after <= level < before) or (after == 0 < before):
            alerts.append({
                "part_number":   d.get("part_number", ""),
                "name":          d.get("name", ""),
                "stock":         after,
                "reorder_level": level,
            })
    return alerts

async def _collect_shortages(needs: dict, session=None):
    """Given aggregated needs, return list of shortages (empty = all OK).
    Each: {part_number, name, have, need}."""
    shortages = []
    for _, need in needs.items():
        fresh = await db.spare_parts.find_one(
            {"_id": need["part"]["_id"]},
            {"stock": 1, "part_number": 1, "name": 1},
            session=session,
        )
        have = int((fresh or {}).get("stock", 0) or 0)
        if have < need["qty"]:
            shortages.append({
                "part_number": (fresh or {}).get("part_number", ""),
                "name":        (fresh or {}).get("name", ""),
                "have":        have,
                "need":        need["qty"],
            })
    return shortages


async def _customer_gstin(customer_id: str) -> str:
    """Customer's GSTIN snapshot for a bill; '' when the customer is missing or has none."""
    try:
        c = await db.customers.find_one({"_id": obj_id(customer_id)}, {"gstin": 1})
    except HTTPException:   # blank / invalid id on old or imported jobs
        return ""
    return ((c or {}).get("gstin") or "").strip().upper()


@api_router.get("/service-bills")
async def list_service_bills(
    job_id: Optional[str] = Query(None),
    p=Depends(paginate_params),
    current_user=Depends(verify_token),
):
    query = {"job_id": job_id} if job_id else {}
    docs  = await db.service_bills.find(query).skip(p["skip"]).limit(p["limit"]).sort("created_at",-1).to_list(p["limit"])
    total = await db.service_bills.count_documents(query)
    return JSONResponse(content=oids(docs), headers={"X-Total-Count": str(total)})

# PATCH 1: Fixed indentation + returns existing bill instead of 409 ────────────
@api_router.post("/service-bills", status_code=201)
async def create_service_bill(body: ServiceBillCreate, current_user=Depends(verify_token)):
    job = await db.service_jobs.find_one({"_id": obj_id(body.job_id)})
    if not job:
        raise HTTPException(status_code=404, detail="Service job not found")
    existing = await db.service_bills.find_one({"job_id": body.job_id})
    if existing:
        # Return the existing bill instead of 409 — frontend uses GET to load anyway
        return JSONResponse(content=oid(existing), status_code=200)

    # Build line items with GST + per-line discount
    items_out = []
    for item in (body.items or []):
        if item.complimentary:
            line = {"gross":0.0,"discount":0.0,"taxable":0.0,"cgst":0.0,"sgst":0.0,"gst_total":0.0,"total":0.0}
            item_disc = 0.0
        else:
            item_disc = max(0.0, float(item.discount or 0))
            line = calc_gst_line(item.unit_price, item.qty, item.gst_rate, item_disc)
        items_out.append({
            "description":   item.description,
            "part_number":   item.part_number or "",
            "hsn_code":      item.hsn_code or "",
            "qty":           item.qty,
            "unit_price":    0 if item.complimentary else item.unit_price,
            "mrp":           item.unit_price if item.complimentary else item.unit_price,
            "gst_rate":      0 if item.complimentary else item.gst_rate,
            "discount":      0 if item.complimentary else item_disc,
            "complimentary": bool(item.complimentary),
            **line,
        })

    # Labour as separate line
    if body.labour_charges and body.labour_charges > 0:
        lab_disc = max(0.0, float(body.labour_discount or 0))
        lab_line = calc_gst_line(body.labour_charges, 1, body.labour_gst_rate or 18, lab_disc)
        items_out.insert(0, {
            "description": "Labour charges",
            "part_number": "", "hsn_code": "9987",
            "qty": 1, "unit_price": body.labour_charges,
            "gst_rate": body.labour_gst_rate or 18,
            "discount": lab_disc,
            **lab_line,
        })

    totals  = calc_bill_totals([{"unit_price": i["unit_price"], "qty": i["qty"], "gst_rate": i["gst_rate"], "discount": i.get("discount", 0)} for i in items_out])
    # Apply discount off grand_total (post-GST). Clamped 0..grand_total.
    discount = max(0.0, min(float(body.discount or 0), totals["grand_total"]))
    net_total = round(totals["grand_total"] - discount, 2)
    # Bill number derives from job number — no separate counter consumed
    bill_no = job.get("job_number", "").replace("SRV", "SRV-B")

    doc = {
        "bill_number":    bill_no,
        "job_id":         body.job_id,
        "job_number":     job.get("job_number",""),
        "customer_id":    job.get("customer_id",""),
        "customer_name":  job.get("customer_name",""),
        "customer_mobile":job.get("customer_mobile",""),
        "customer_gstin": await _customer_gstin(job.get("customer_id","")),
        "vehicle_number": job.get("vehicle_number",""),
        "brand":          job.get("brand",""),
        "model":          job.get("model",""),
        "items":          items_out,
        "labour_charges": body.labour_charges or 0,
        **totals,
        "discount":       discount,
        "net_total":      net_total,
        "amount_in_words": amount_in_words(net_total),
        "payment_mode":   body.payment_mode or "Cash",
        "notes":          body.notes or "",
        "bill_date":      ist_now().strftime("%d %b %Y"),
        "created_at":     utcnow().isoformat(),
    }

    # ── Transactional: deduct stock + insert bill + update job (all-or-nothing) ──
    # Any 409 raised inside → txn aborts → zero writes committed. First txn use in
    # codebase; parts_bills has the same partial-state bug and needs the same fix.
    inserted_id = None
    touched: dict = {}
    async with await _db.client.start_session() as session:
        async def _txn(s):
            nonlocal inserted_id
            touched.clear()   # a retried transaction must not double-count
            needs = await _aggregate_bill_needs(body.items or [], session=s)
            shortages = await _collect_shortages(needs, session=s)
            if shortages:
                raise HTTPException(status_code=409, detail={
                    "error": "insufficient_stock",
                    "shortages": shortages,
                })
            # Guarded deduct — defense-in-depth vs concurrent bill (rare inside txn but cheap)
            for _, need in needs.items():
                part_id = need["part"]["_id"]
                qty     = need["qty"]
                dec = await db.spare_parts.update_one(
                    {"_id": part_id, "stock": {"$gte": qty}},
                    {"$inc": {"stock": -qty}, "$push": {"stock_log": {
                        "qty": -qty, "action": "subtract",
                        "reason": "service_bill", "bill_number": bill_no,
                        "date": utcnow().isoformat(),
                    }}},
                    session=s,
                )
                if dec.matched_count == 0:
                    fresh = await db.spare_parts.find_one(
                        {"_id": part_id}, {"stock": 1, "name": 1, "part_number": 1}, session=s
                    )
                    raise HTTPException(status_code=409, detail={
                        "error": "insufficient_stock",
                        "shortages": [{
                            "part_number": (fresh or {}).get("part_number", ""),
                            "name":        (fresh or {}).get("name", ""),
                            "have":        int((fresh or {}).get("stock", 0) or 0),
                            "need":        qty,
                        }],
                    })
                touched[part_id] = touched.get(part_id, 0) + qty
            result = await db.service_bills.insert_one(doc, session=s)
            inserted_id = result.inserted_id
            await db.service_jobs.update_one(
                {"_id": obj_id(body.job_id)},
                {"$set": {"status": "ready", "bill_number": doc["bill_number"], "grand_total": net_total}},
                session=s,
            )
        await session.with_transaction(_txn)

    doc["id"] = str(inserted_id); doc.pop("_id", None)
    doc["low_stock_alerts"] = await _priority_low_alerts(touched)
    return doc

@api_router.get("/service-bills/{bill_id}")
async def get_service_bill(bill_id: str, current_user=Depends(verify_token)):
    doc = await db.service_bills.find_one({"_id": obj_id(bill_id)})
    if not doc:
        doc = await db.service_bills.find_one({"bill_number": bill_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Bill not found")
    return oid(doc)

# PATCH 2: PUT /service-bills/{bill_id} — was a string variable, now real code ─
@api_router.put("/service-bills/{bill_id}")
async def update_service_bill(bill_id: str, body: ServiceBillCreate, current_user=Depends(verify_token)):
    bill = await db.service_bills.find_one({"_id": obj_id(bill_id)})
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")

    items_out = []
    for item in (body.items or []):
        if item.complimentary:
            line = {"gross":0.0,"discount":0.0,"taxable":0.0,"cgst":0.0,"sgst":0.0,"gst_total":0.0,"total":0.0}
            item_disc = 0.0
        else:
            item_disc = max(0.0, float(item.discount or 0))
            line = calc_gst_line(item.unit_price, item.qty, item.gst_rate, item_disc)
        items_out.append({
            "description":   item.description,
            "part_number":   item.part_number or "",
            "hsn_code":      item.hsn_code or "",
            "qty":           item.qty,
            "unit_price":    0 if item.complimentary else item.unit_price,
            "mrp":           item.unit_price,
            "gst_rate":      0 if item.complimentary else item.gst_rate,
            "discount":      0 if item.complimentary else item_disc,
            "complimentary": bool(item.complimentary),
            **line,
        })

    totals = calc_bill_totals([
        {"unit_price": i["unit_price"], "qty": i["qty"], "gst_rate": i["gst_rate"], "discount": i.get("discount", 0)}
        for i in items_out
    ])
    # Discount: use new value if provided, else keep existing
    discount_raw = body.discount if body.discount is not None else bill.get("discount", 0)
    discount = max(0.0, min(float(discount_raw or 0), totals["grand_total"]))
    net_total = round(totals["grand_total"] - discount, 2)

    update = {
        "items":           items_out,
        "payment_mode":    body.payment_mode or bill.get("payment_mode", "Cash"),
        "updated_at":      utcnow(),
        "discount":        discount,
        "net_total":       net_total,
        "amount_in_words": amount_in_words(net_total),
        **totals,
    }

    # ── Transactional: restore old stock, deduct new stock, update bill+job ──
    # Restore-then-deduct pattern (matches parts_bills), but atomically inside a
    # session. If new items have shortages after restore, txn aborts → old stock
    # is preserved, bill unchanged.
    #
    # Build fake "items" iterable for old bill (dicts, not Pydantic) — helpers
    # use getattr so we wrap in a light shim.
    class _OldItem:
        __slots__ = ("description", "part_number", "qty")
        def __init__(self, d):
            self.description = d.get("description", "") or ""
            self.part_number = d.get("part_number", "") or ""
            self.qty         = d.get("qty", 0) or 0
    old_items = [_OldItem(d) for d in bill.get("items", [])]
    bill_no   = bill.get("bill_number", "")

    touched: dict = {}
    async with await _db.client.start_session() as session:
        async def _txn(s):
            touched.clear()   # a retried transaction must not double-count
            # Step 1: restore stock from old items
            old_needs = await _aggregate_bill_needs(old_items, session=s)
            for _, need in old_needs.items():
                await db.spare_parts.update_one(
                    {"_id": need["part"]["_id"]},
                    {"$inc": {"stock": need["qty"]}, "$push": {"stock_log": {
                        "qty": need["qty"], "action": "add",
                        "reason": "service_bill_edit_restore", "bill_number": bill_no,
                        "date": utcnow().isoformat(),
                    }}},
                    session=s,
                )
            # Step 2: check shortages against new items (stock now reflects restores)
            new_needs = await _aggregate_bill_needs(body.items or [], session=s)
            shortages = await _collect_shortages(new_needs, session=s)
            if shortages:
                raise HTTPException(status_code=409, detail={
                    "error": "insufficient_stock",
                    "shortages": shortages,
                })
            # Step 3: deduct new items (guarded)
            for _, need in new_needs.items():
                part_id = need["part"]["_id"]
                qty     = need["qty"]
                dec = await db.spare_parts.update_one(
                    {"_id": part_id, "stock": {"$gte": qty}},
                    {"$inc": {"stock": -qty}, "$push": {"stock_log": {
                        "qty": -qty, "action": "subtract",
                        "reason": "service_bill_edit", "bill_number": bill_no,
                        "date": utcnow().isoformat(),
                    }}},
                    session=s,
                )
                if dec.matched_count == 0:
                    fresh = await db.spare_parts.find_one(
                        {"_id": part_id}, {"stock": 1, "name": 1, "part_number": 1}, session=s
                    )
                    raise HTTPException(status_code=409, detail={
                        "error": "insufficient_stock",
                        "shortages": [{
                            "part_number": (fresh or {}).get("part_number", ""),
                            "name":        (fresh or {}).get("name", ""),
                            "have":        int((fresh or {}).get("stock", 0) or 0),
                            "need":        qty,
                        }],
                    })
            # Net removal per part across the edit — only a net decrease can cross the level
            for key, need in new_needs.items():
                net = need["qty"] - (old_needs.get(key, {}).get("qty", 0) or 0)
                if net > 0:
                    touched[need["part"]["_id"]] = net
            # Step 4: persist bill + job update
            await db.service_bills.update_one({"_id": obj_id(bill_id)}, {"$set": update}, session=s)
            await db.service_jobs.update_one(
                {"_id": obj_id(bill["job_id"])},
                {"$set": {"grand_total": net_total}},
                session=s,
            )
        await session.with_transaction(_txn)

    updated = await db.service_bills.find_one({"_id": obj_id(bill_id)})
    payload = oid(updated)
    payload["low_stock_alerts"] = await _priority_low_alerts(touched)
    return JSONResponse(content=payload)

@api_router.delete("/service-bills/{bill_id}")
async def delete_service_bill(bill_id: str, current_user=Depends(require_admin)):
    bill = await db.service_bills.find_one({"_id": obj_id(bill_id)})
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")
    # Restore stock for all parts used in this bill (audit-logged)
    bill_no = bill.get("bill_number", "")
    for item in bill.get("items", []):
        desc = (item.get("description") or "").strip()
        pn   = (item.get("part_number") or "").strip()
        qty  = int(item.get("qty") or 0)
        if qty <= 0:
            continue
        part = await _resolve_spare_part(desc, pn)
        if not part:
            continue
        await db.spare_parts.update_one(
            {"_id": part["_id"]},
            {"$inc": {"stock": qty}, "$push": {"stock_log": {
                "qty": qty, "action": "add",
                "reason": "service_bill_delete", "bill_number": bill_no,
                "date": utcnow().isoformat(),
            }}}
        )
    await db.service_bills.delete_one({"_id": obj_id(bill_id)})
    await db.service_jobs.update_one(
        {"_id": obj_id(bill["job_id"])},
        {"$set": {"status": "in_progress"}, "$unset": {"bill_number": ""}}
    )
    return {"message": "Deleted"}
