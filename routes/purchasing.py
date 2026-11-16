"""API routes: purchasing (split out of server.py; code moved verbatim)."""
import json
import re
from datetime import datetime
from typing import Optional, List
from fastapi import Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import JSONResponse
import os
from database import verify_token, require_admin, next_sequence, oid, oids, obj_id, paginate_params, utcnow
from fastapi import File, UploadFile
from routes.files import MAX_UPLOAD_BYTES, _sniff_upload_type
from models import PurchaseBillCreate, PurchaseBillUpdate, VendorCreate, VendorUpdate
from routers import api_router

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

# ═══════════════════════════════════════════════════════════════════════════════
#  VENDORS  (spare-parts suppliers)
# ═══════════════════════════════════════════════════════════════════════════════

@api_router.get("/vendors")
async def list_vendors(
    search: Optional[str] = Query(None),
    current_user=Depends(verify_token),
):
    query: dict = {}
    if search:
        esc = re.escape(search)
        query["$or"] = [
            {"name":  {"$regex": esc, "$options": "i"}},
            {"gstin": {"$regex": esc, "$options": "i"}},
            {"phone": {"$regex": esc, "$options": "i"}},
        ]
    docs = await db.vendors.find(query).sort("name", 1).to_list(500)
    return oids(docs)

@api_router.post("/vendors", status_code=201)
async def create_vendor(body: VendorCreate, current_user=Depends(verify_token)):
    doc = body.dict()
    doc["name"] = doc["name"].strip()
    if not doc["name"]:
        raise HTTPException(status_code=400, detail="Vendor name required")
    doc["gstin"] = (doc.get("gstin") or "").strip().upper()
    # Soft-uniqueness: warn if same name or GSTIN already exists, but don't block
    doc["created_at"] = utcnow().isoformat()
    result = await db.vendors.insert_one(doc)
    doc["id"] = str(result.inserted_id); doc.pop("_id", None)
    return doc

@api_router.get("/vendors/{vendor_id}")
async def get_vendor(vendor_id: str, current_user=Depends(verify_token)):
    doc = await db.vendors.find_one({"_id": obj_id(vendor_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return oid(doc)

@api_router.put("/vendors/{vendor_id}")
async def update_vendor(vendor_id: str, body: VendorUpdate, current_user=Depends(verify_token)):
    update = {k: v for k, v in body.dict().items() if v is not None}
    if "name" in update:
        update["name"] = update["name"].strip()
    if "gstin" in update:
        update["gstin"] = (update["gstin"] or "").strip().upper()
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")
    r = await db.vendors.update_one({"_id": obj_id(vendor_id)}, {"$set": update})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return oid(await db.vendors.find_one({"_id": obj_id(vendor_id)}))

@api_router.delete("/vendors/{vendor_id}")
async def delete_vendor(vendor_id: str, current_user=Depends(require_admin)):
    # Block delete if any purchase bills reference this vendor
    ref_count = await db.purchase_bills.count_documents({"vendor_id": vendor_id})
    if ref_count > 0:
        raise HTTPException(status_code=409,
            detail=f"Cannot delete: {ref_count} purchase bill(s) reference this vendor")
    r = await db.vendors.delete_one({"_id": obj_id(vendor_id)})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return {"deleted": True}

@api_router.get("/vendors/{vendor_id}/summary")
async def vendor_summary(vendor_id: str, current_user=Depends(verify_token)):
    """Total spend + bill count + last-bill date for this vendor."""
    pipeline = [
        {"$match": {"vendor_id": vendor_id}},
        {"$group": {
            "_id": None,
            "total_spend":  {"$sum": "$grand_total"},
            "bill_count":   {"$sum": 1},
            "last_bill":    {"$max": "$bill_date"},
        }},
    ]
    result = await db.purchase_bills.aggregate(pipeline).to_list(1)
    if not result:
        return {"total_spend": 0, "bill_count": 0, "last_bill": None}
    r = result[0]
    r.pop("_id", None)
    return r


# ═══════════════════════════════════════════════════════════════════════════════
#  PURCHASE BILLS  (incoming stock from vendors)
# ═══════════════════════════════════════════════════════════════════════════════

def _pb_compute_line(item: dict) -> dict:
    """Compute taxable_amt, cgst, sgst, line_total for one bill line.
    Rate is the per-unit price BEFORE discount. GST is added on top of the
    discounted taxable amount (standard trade invoice convention)."""
    qty          = float(item.get("qty") or 0)
    rate         = float(item.get("rate") or 0)
    discount_pct = float(item.get("discount_pct") or 0)
    gst_pct      = float(item.get("gst_pct") or 0)
    gross        = qty * rate
    discount     = gross * (discount_pct / 100.0)
    taxable      = round(gross - discount, 2)
    gst_total    = round(taxable * (gst_pct / 100.0), 2)
    cgst         = round(gst_total / 2, 2)
    sgst         = round(gst_total - cgst, 2)  # keep balance exact
    line_total   = round(taxable + cgst + sgst, 2)
    return {**item,
            "taxable_amt": taxable, "cgst": cgst, "sgst": sgst,
            "line_total":  line_total}

def _pb_compute_totals(items: List[dict], round_off: float = 0.0) -> dict:
    subtotal    = round(sum(i.get("taxable_amt", 0) for i in items), 2)
    total_cgst  = round(sum(i.get("cgst", 0)        for i in items), 2)
    total_sgst  = round(sum(i.get("sgst", 0)        for i in items), 2)
    grand       = round(subtotal + total_cgst + total_sgst + (round_off or 0), 2)
    return {"subtotal": subtotal, "total_cgst": total_cgst,
            "total_sgst": total_sgst, "grand_total": grand,
            "round_off": round(round_off or 0, 2)}

async def _pb_apply_stock_deltas(items: List[dict], vendor_id: str, vendor_name: str,
                                  bill_date: str, sign: int = +1):
    """Apply +qty (sign=+1) or -qty (sign=-1) to stock for each line.
    Also update aliases + last_purchase_rate on the linked part."""
    for it in items:
        part_id = it.get("part_id")
        if not part_id: continue
        qty     = float(it.get("qty") or 0) * sign
        # Net cost per unit AFTER discount, INCLUDING GST (what you actually paid)
        line_total = float(it.get("line_total") or 0)
        base_qty   = float(it.get("qty") or 0)
        net_unit   = round(line_total / base_qty, 2) if base_qty else 0
        try:
            oid_p = obj_id(part_id)
        except HTTPException:
            continue
        update: dict = {"$inc": {"stock": int(qty)}}
        # Only update purchase_price/aliases/last_purchase_rate on the ADD path
        if sign > 0:
            update["$set"] = {
                "last_purchase_rate":     net_unit,
                "last_purchase_date":     bill_date,
                "last_purchase_vendor":   vendor_name,
            }
            # Delivery arrived — the part is no longer awaiting an order.
            update["$unset"] = {"ordered_at": "", "ordered_by": ""}
            # Store this vendor's naming as an alias (upsert-style: add if new)
            alias_entry = {
                "vendor_id":    vendor_id,
                "vendor_name":  vendor_name,
                "part_number":  (it.get("part_number") or "").strip(),
                "part_name":    (it.get("part_name")   or "").strip(),
            }
            # Only add alias if part_number or part_name is non-empty AND not already present
            if alias_entry["part_number"] or alias_entry["part_name"]:
                await db.spare_parts.update_one(
                    {"_id": oid_p, "aliases": {
                        "$not": {"$elemMatch": {
                            "vendor_id":   vendor_id,
                            "part_number": alias_entry["part_number"],
                        }}
                    }},
                    {"$push": {"aliases": alias_entry}},
                )
            # Optional caller-driven sale-price change
            if it.get("new_sale_price") is not None:
                update["$set"]["selling_price"] = float(it["new_sale_price"])
        await db.spare_parts.update_one({"_id": oid_p}, update)

async def _pb_create_new_parts(items: List[dict], current_user: dict) -> List[dict]:
    """For lines with part_id=None: create a fresh spare_part and stamp part_id on the item.
    Returns the mutated items list."""
    out = []
    for it in items:
        item = dict(it)
        if item.get("part_id"):
            out.append(item); continue
        # Auto-generate part number if vendor didn't provide one
        pn = (item.get("part_number") or "").strip()
        if not pn:
            pn = await next_sequence("part")
        else:
            # Collision safety: append seq if number already taken
            if await db.spare_parts.find_one({"part_number": pn}):
                pn = f"{pn}-{await next_sequence('part')}"
        line_total = float(item.get("line_total") or 0)
        base_qty   = float(item.get("qty") or 0)
        net_unit   = round(line_total / base_qty, 2) if base_qty else 0
        # Default selling price = net_unit * 1.25 (25% margin) unless caller overrode
        selling  = item.get("new_part_selling_price")
        if selling is None:
            selling = round(net_unit * 1.25, 2)
        new_part = {
            "part_number":     pn,
            "name":            (item.get("part_name") or "").strip(),
            "category":        "",
            "brand":           "",
            "compatible_with": [],
            "stock":           0,   # will get incremented by _pb_apply_stock_deltas
            "reorder_level":   5,
            "purchase_price":  net_unit,
            "selling_price":   float(selling),
            "gst_rate":        float(item.get("gst_pct") or 18),
            "hsn_code":        (item.get("hsn") or "").strip(),
            "location":        "",
            "aliases":         [],
            "created_at":      utcnow().isoformat(),
            "created_by":      current_user.get("username", ""),
        }
        res = await db.spare_parts.insert_one(new_part)
        item["part_id"] = str(res.inserted_id)
        out.append(item)
    return out

# ── Reading a supplier bill (PDF / photo) ─────────────────────────────────────
# Claude reads the bill into the same shape the New Purchase Bill form uses. Nothing is
# saved here: the form opens pre-filled and staff check every line before Save.
_BILL_UNITS = ["PCS", "PC", "SET", "NOS", "EACH", "KG", "LTR"]   # the form's unit list
_BILL_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor_name":   {"type": "string"},
        "vendor_gstin":  {"type": "string"},
        "bill_number":   {"type": "string"},
        "bill_date":     {"type": "string", "description": "YYYY-MM-DD"},
        "other_charges": {"type": "number", "description": "freight, forwarding, packing etc. added below the items"},
        "grand_total":   {"type": "number"},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "description":  {"type": "string"},
                "part_number":  {"type": "string"},
                "hsn":          {"type": "string"},
                "qty":          {"type": "number"},
                "unit":         {"type": "string", "enum": _BILL_UNITS},
                "mrp":          {"type": "number"},
                "discount_pct": {"type": "number"},
                "unit_price":   {"type": "number", "description": "taxable price per unit: after discount, before GST"},
                "gst_pct":      {"type": "number", "description": "total GST %, e.g. CGST 9 + SGST 9 = 18"},
                "amount":       {"type": "number", "description": "the line's amount column as printed"},
            },
            "required": ["description", "part_number", "hsn", "qty", "unit", "mrp",
                         "discount_pct", "unit_price", "gst_pct", "amount"],
            "additionalProperties": False,
        }},
    },
    "required": ["vendor_name", "vendor_gstin", "bill_number", "bill_date",
                 "other_charges", "grand_total", "items"],
    "additionalProperties": False,
}
_BILL_PROMPT = """This is a supplier's tax invoice for spare parts bought by a motorcycle dealer in India.
Extract the header and every line item exactly as printed. It may be a scan, so ignore pen ticks
and stamps, and read numbers carefully.
- Use "" for a text field that is blank on the bill (many lines have no part number) and 0 for a missing number.
- unit: map the printed unit to the closest allowed value (Pcs -> PCS, Kit/Set -> SET, Nos -> NOS).
- bill_date as YYYY-MM-DD. other_charges is the sum of charges added after the items (freight, forwarding, packing)."""

_anthropic_client = None

async def _extract_bill(data: bytes, content_type: str) -> dict:
    """Ask Claude to read the bill; returns a dict matching _BILL_SCHEMA."""
    import base64
    import anthropic
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic()
    b64 = base64.standard_b64encode(data).decode("ascii")
    source_block = (
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
        if content_type == "application/pdf" else
        {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": b64}}
    )
    try:
        resp = await _anthropic_client.messages.create(
            model="claude-opus-5-5",
            max_tokens=16000,
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": _BILL_SCHEMA}},
            messages=[{"role": "user", "content": [source_block, {"type": "text", "text": _BILL_PROMPT}]}],
        )
    except anthropic.AuthenticationError:
        raise HTTPException(status_code=503, detail="Bill reading: the ANTHROPIC_API_KEY on the server is not valid")
    except anthropic.RateLimitError:
        raise HTTPException(status_code=429, detail="Bill reading is busy, try again in a minute")
    except anthropic.APIStatusError as e:
        # Anthropic's own reason (e.g. "credit balance is too low") is what tells staff what to fix
        reason = ((e.body or {}).get("error") or {}).get("message", "") if isinstance(e.body, dict) else ""
        print(f"[MM Motors] Bill reading failed: {e.status_code} {reason or e.message}")
        raise HTTPException(status_code=502, detail=f"Bill reading failed ({e.status_code}): {reason or 'try again'}")
    except anthropic.APIConnectionError:
        raise HTTPException(status_code=502, detail="Could not reach the bill reader, try again")
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise HTTPException(status_code=422, detail="Could not read this bill, enter it by hand")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)

def _bill_date(iso: str) -> str:
    try:
        return datetime.strptime(iso.strip(), "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return iso.strip()

def _exact(s: str) -> dict:
    return {"$regex": f"^{re.escape(s.strip())}$", "$options": "i"}

async def _match_bill_part(line: dict, vendor_id: Optional[str], repeated_names: set) -> Optional[dict]:
    """Link a bill line to an existing part only when exactly one part fits: by part number
    first, else by name. A name printed on several lines of the same bill (e.g. ten different
    "Map Air Filter"s) is never matched by name, since one part can't be all of them."""
    def alias(field, value):
        return [{"aliases": {"$elemMatch": {"vendor_id": vendor_id, field: _exact(value)}}}] if vendor_id else []
    pn, name = line["part_number"].strip(), line["description"].strip()
    if pn:
        query = {"$or": [{"part_number": _exact(pn)}, *alias("part_number", pn)]}
    elif name and name.upper() not in repeated_names:
        query = {"$or": [{"name": _exact(name)}, *alias("part_name", name)]}
    else:
        return None
    found = await db.spare_parts.find(query).limit(2).to_list(2)
    return found[0] if len(found) == 1 else None

@api_router.post("/purchase-bills/parse")
async def parse_purchase_bill(file: UploadFile = File(...), current_user=Depends(verify_token)):
    """Read a supplier bill (PDF or photo) into a draft for the New Purchase Bill form.
    Saves nothing: stock only changes when staff save the reviewed form."""
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    content_type = _sniff_upload_type(data[:16])
    if not content_type:
        raise HTTPException(status_code=415, detail="Upload the bill as a PDF, JPEG, PNG or WebP")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=503, detail="Bill reading is not set up: add ANTHROPIC_API_KEY on the server")

    bill = await _extract_bill(data, content_type)

    gstin  = bill["vendor_gstin"].strip().upper()
    vendor = (await db.vendors.find_one({"gstin": _exact(gstin)}) if gstin else None) \
             or await db.vendors.find_one({"name": _exact(bill["vendor_name"])})
    vendor_id = str(vendor["_id"]) if vendor else None

    names = [i["description"].strip().upper() for i in bill["items"] if not i["part_number"].strip()]
    repeated = {n for n in names if names.count(n) > 1}
    items = []
    for line in bill["items"]:
        gst  = line["gst_pct"]
        rate = line["unit_price"] or (line["amount"] / line["qty"] / (1 + gst / 100) if line["qty"] else 0)
        part = await _match_bill_part(line, vendor_id, repeated)
        row = {
            "part_id":      str(part["_id"]) if part else None,
            "part_name":    part["name"] if part else line["description"].strip(),
            "part_number":  line["part_number"].strip(),
            "hsn":          line["hsn"].strip() or (part or {}).get("hsn_code", ""),
            "qty":          line["qty"],
            "unit":         line["unit"],
            "rate":         round(rate, 2),
            "discount_pct": 0,   # rate is already the discounted price
            "gst_pct":      gst,
            "mrp":          line["mrp"],
            "bill_text":    line["description"].strip(),
        }
        if part:   # lets the form flag a changed purchase cost, as when a part is picked by hand
            row.update({"_existing_rate": part.get("last_purchase_rate") or part.get("purchase_price"),
                        "_existing_selling": part.get("selling_price"), "_stock": part.get("stock")})
        items.append(row)

    other = round(bill["other_charges"] or 0, 2)
    return {
        "vendor_id":    vendor_id,
        "vendor_name":  bill["vendor_name"].strip(),
        "vendor_gstin": gstin,
        "bill_number":  bill["bill_number"].strip(),
        "bill_date":    _bill_date(bill["bill_date"]),
        "round_off":    other,
        "notes":        f"Freight/other charges Rs {other} included in round-off" if other else "",
        "grand_total":  bill["grand_total"],
        "items":        items,
    }

@api_router.get("/purchase-bills")
async def list_purchase_bills(
    vendor_id: Optional[str] = Query(None),
    search:    Optional[str] = Query(None),
    p=Depends(paginate_params),
    current_user=Depends(verify_token),
):
    query: dict = {}
    if vendor_id: query["vendor_id"] = vendor_id
    if search:
        esc = re.escape(search)
        query["$or"] = [
            {"bill_number": {"$regex": esc, "$options": "i"}},
            {"vendor_name": {"$regex": esc, "$options": "i"}},
        ]
    docs  = await db.purchase_bills.find(query).skip(p["skip"]).limit(p["limit"]).sort("created_at", -1).to_list(p["limit"])
    total = await db.purchase_bills.count_documents(query)
    return JSONResponse(content=oids(docs), headers={"X-Total-Count": str(total)})

@api_router.get("/purchase-bills/{bill_id}")
async def get_purchase_bill(bill_id: str, current_user=Depends(verify_token)):
    doc = await db.purchase_bills.find_one({"_id": obj_id(bill_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Purchase bill not found")
    return oid(doc)

@api_router.post("/purchase-bills", status_code=201)
async def create_purchase_bill(body: PurchaseBillCreate, current_user=Depends(verify_token)):
    # Fetch vendor (denorm name onto bill)
    vendor = await db.vendors.find_one({"_id": obj_id(body.vendor_id)})
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    if not body.items:
        raise HTTPException(status_code=400, detail="Bill must have at least one line")

    # Duplicate check: same vendor + same bill_number
    bill_num = body.bill_number.strip()
    if not bill_num:
        raise HTTPException(status_code=400, detail="Bill number required")
    dup = await db.purchase_bills.find_one({"vendor_id": body.vendor_id, "bill_number": bill_num})
    if dup:
        raise HTTPException(status_code=409, detail=f"Bill number {bill_num} already recorded for this vendor")

    # Compute per-line taxes + totals
    items = [_pb_compute_line(i.dict()) for i in body.items]
    # Auto-create any new parts (part_id=None); returns items with part_id filled
    items = await _pb_create_new_parts(items, current_user)
    totals = _pb_compute_totals(items, body.round_off or 0)

    vendor_name = vendor.get("name", "")
    doc = {
        "vendor_id":       body.vendor_id,
        "vendor_name":     vendor_name,
        "vendor_gstin":    vendor.get("gstin", ""),
        "bill_number":     bill_num,
        "bill_date":       body.bill_date.strip(),
        "place_of_supply": body.place_of_supply or "",
        "items":           items,
        "notes":           body.notes or "",
        **totals,
        "created_at":      utcnow().isoformat(),
        "created_by":      current_user.get("username", ""),
    }
    result = await db.purchase_bills.insert_one(doc)
    doc["id"] = str(result.inserted_id); doc.pop("_id", None)

    # Apply stock deltas + aliases + last_purchase_rate
    await _pb_apply_stock_deltas(items, body.vendor_id, vendor_name, body.bill_date.strip(), sign=+1)
    return doc

@api_router.put("/purchase-bills/{bill_id}")
async def update_purchase_bill(bill_id: str, body: PurchaseBillUpdate,
                               current_user=Depends(require_admin)):
    """Owner-only. Reverses old stock deltas, then applies new."""
    existing = await db.purchase_bills.find_one({"_id": obj_id(bill_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Purchase bill not found")

    updates = {k: v for k, v in body.dict().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")

    # Reverse old stock
    await _pb_apply_stock_deltas(
        existing.get("items") or [],
        existing.get("vendor_id"),
        existing.get("vendor_name", ""),
        existing.get("bill_date", ""),
        sign=-1,
    )

    # Rebuild items if provided
    if "items" in updates:
        new_items = [_pb_compute_line(dict(i) if isinstance(i, dict) else i.dict())
                     for i in updates["items"]]
        new_items = await _pb_create_new_parts(new_items, current_user)
        updates["items"] = new_items
        round_off = updates.get("round_off", existing.get("round_off", 0))
        updates.update(_pb_compute_totals(new_items, round_off or 0))

    updates["updated_at"] = utcnow().isoformat()
    updates["updated_by"] = current_user.get("username", "")
    await db.purchase_bills.update_one({"_id": obj_id(bill_id)}, {"$set": updates})

    # Re-fetch fresh doc and re-apply stock in +direction
    fresh = await db.purchase_bills.find_one({"_id": obj_id(bill_id)})
    await _pb_apply_stock_deltas(
        fresh.get("items") or [],
        fresh.get("vendor_id"),
        fresh.get("vendor_name", ""),
        fresh.get("bill_date", ""),
        sign=+1,
    )
    return oid(fresh)

@api_router.delete("/purchase-bills/{bill_id}")
async def delete_purchase_bill(bill_id: str, current_user=Depends(require_admin)):
    """Owner-only. Reverses stock, then deletes."""
    existing = await db.purchase_bills.find_one({"_id": obj_id(bill_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Purchase bill not found")
    await _pb_apply_stock_deltas(
        existing.get("items") or [],
        existing.get("vendor_id"),
        existing.get("vendor_name", ""),
        existing.get("bill_date", ""),
        sign=-1,
    )
    await db.purchase_bills.delete_one({"_id": obj_id(bill_id)})
    return {"deleted": True}


# ─── Parts search: vendor-aware fuzzy match (for purchase-bill autocomplete) ──
@api_router.get("/parts/search-alias")
async def parts_search_alias(
    q:         str          = Query(..., min_length=1),
    vendor_id: Optional[str] = Query(None),
    limit:     int          = Query(20, ge=1, le=100),
    current_user=Depends(verify_token),
):
    """Search parts by name, part_number, OR alias.part_number (vendor-scoped).
    Returns up to `limit` matches; vendor-alias hits ranked first when vendor_id passed."""
    esc = re.escape(q)
    # Alias-match branch (vendor-scoped when vendor_id provided)
    alias_branch = {"aliases": {"$elemMatch": {
        "$or": [
            {"part_number": {"$regex": esc, "$options": "i"}},
            {"part_name":   {"$regex": esc, "$options": "i"}},
        ]
    }}}
    if vendor_id:
        alias_branch["aliases"]["$elemMatch"]["vendor_id"] = vendor_id
    query = {"$or": [
        {"name":        {"$regex": esc, "$options": "i"}},
        {"part_number": {"$regex": esc, "$options": "i"}},
        alias_branch,
    ]}
    docs = await db.spare_parts.find(query).limit(limit).to_list(limit)
    return oids(docs)
