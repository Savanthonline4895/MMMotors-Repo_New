"""API routes: reports (split out of server.py; code moved verbatim)."""
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
import io
from database import verify_token, require_admin, oids, obj_id, utcnow, ist_now
import io
from openpyxl import Workbook
from fastapi.responses import StreamingResponse
from common import SALE_MILESTONE_KEYS
from routers import api_router
from routes.vehicle_bookings import _in_stock_vehicles, booking_matches

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

# ═══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD STATS
# ═══════════════════════════════════════════════════════════════════════════════

async def _bookings_ready() -> int:
    """Waiting bike bookings whose model is now in stock — the customer should be called."""
    waiting = await db.vehicle_bookings.find({"status": "waiting"}, {"brand": 1, "model": 1, "color": 1}).to_list(None)
    if not waiting:
        return 0
    stock = await _in_stock_vehicles()
    return sum(1 for b in waiting if booking_matches(b, stock))

@api_router.get("/dashboard/stats")
async def dashboard_stats(current_user=Depends(verify_token)):
    now = ist_now()
    today_legacy = now.strftime("%d %b %Y")   # legacy format: "04 Sep 2026"
    today_iso    = now.strftime("%Y-%m-%d")   # new format:    "2026-09-04"
    month_legacy = now.strftime("%b %Y")      # legacy month:  "Sep 2026"
    month_iso    = now.strftime("%Y-%m")      # new month:     "2026-09"
    today_match  = {"sale_date": {"$in": [today_legacy, today_iso]}}
    month_match  = {"$or": [
        {"sale_date": {"$regex": month_legacy}},
        {"sale_date": {"$regex": f"^{month_iso}"}},
    ]}
    (vehicles_in_stock, vehicles_sold_today, jobs_pending, jobs_in_progress, jobs_ready, customers_total, parts_low, parts_out, sales_today_count) = await asyncio.gather(
        db.vehicles.count_documents({"status":"in_stock"}),
        db.sales.count_documents(today_match),
        db.service_jobs.count_documents({"status":"pending"}),
        db.service_jobs.count_documents({"status":"in_progress"}),
        db.service_jobs.count_documents({"status":"ready"}),
        db.customers.count_documents({}),
        db.spare_parts.count_documents({"$expr":{"$and":[{"$gt":["$stock",0]},{"$lte":["$stock","$reorder_level"]}]}}),
        db.spare_parts.count_documents({"stock":0}),
        db.sales.count_documents(today_match),
    )
    pipeline_today_rev = [{"$match":today_match},{"$group":{"_id":None,"total":{"$sum":"$total_amount"}}}]
    pipeline_month_rev = [{"$match":month_match},{"$group":{"_id":None,"total":{"$sum":"$total_amount"}}}]
    today_rev_r, month_rev_r = await asyncio.gather(
        db.sales.aggregate(pipeline_today_rev).to_list(1),
        db.sales.aggregate(pipeline_month_rev).to_list(1),
    )
    return {
        "vehicles":  {"in_stock":vehicles_in_stock,"sold_today":vehicles_sold_today},
        "service":   {"pending":jobs_pending,"in_progress":jobs_in_progress,"ready":jobs_ready,"active_total":jobs_pending+jobs_in_progress+jobs_ready},
        "customers": customers_total,
        "parts":     {"low_stock":parts_low,"out_of_stock":parts_out},
        "bookings_ready": await _bookings_ready(),
        "revenue":   {"today":today_rev_r[0]["total"] if today_rev_r else 0,"month":month_rev_r[0]["total"] if month_rev_r else 0},
        "sales_today_count": sales_today_count,
    }

@api_router.get("/dashboard/recent-activity")
async def recent_activity(limit: int = Query(10, le=50), current_user=Depends(verify_token)):
    sales_docs, job_docs, bill_docs = await asyncio.gather(
        db.sales.find({}).sort("created_at",-1).limit(limit).to_list(limit),
        db.service_jobs.find({}).sort("created_at",-1).limit(limit).to_list(limit),
        db.parts_sales.find({}).sort("created_at",-1).limit(limit).to_list(limit),
    )
    activity = []
    for s in sales_docs:
        activity.append({"type":"sale","time":s.get("created_at",""),"text":s.get("invoice_number",""),"sub":f"{s.get('customer_name','')} · ₹{s.get('total_amount',0):,.0f}"})
    for j in job_docs:
        activity.append({"type":"service","time":j.get("created_at",""),"text":j.get("job_number","")+" "+j.get("status",""),"sub":f"{j.get('vehicle_number','')} · {j.get('customer_name','')}"})
    for b in bill_docs:
        activity.append({"type":"parts","time":b.get("created_at",""),"text":b.get("bill_number",""),"sub":f"{b.get('customer_name','')} · ₹{b.get('grand_total',0):,.0f}"})
    activity.sort(key=lambda x: x["time"], reverse=True)
    return activity[:limit]


# ═══════════════════════════════════════════════════════════════════════════════
#  REPORTS
# ═══════════════════════════════════════════════════════════════════════════════

@api_router.get("/reports/revenue")
async def revenue_report(months: int = Query(6, ge=1, le=24), current_user=Depends(require_admin)):
    # sale_date can be: "08 Apr 2026", "2026-04-24", "2024-07-01 00:00:00", "01/04/2026"
    # Strategy: try multiple formats, fall back to created_at substring
    pipeline = [
        {"$addFields": {
            # Try "DD Mon YYYY" e.g. "08 Apr 2026"
            "p1": {"$dateFromString": {"dateString": "$sale_date", "format": "%d %b %Y", "onError": None, "onNull": None}},
            # Try "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS"
            "p2": {"$dateFromString": {"dateString": {"$substr": ["$sale_date", 0, 10]}, "format": "%Y-%m-%d", "onError": None, "onNull": None}},
            # Try "DD/MM/YYYY"
            "p3": {"$dateFromString": {"dateString": "$sale_date", "format": "%d/%m/%Y", "onError": None, "onNull": None}},
        }},
        {"$addFields": {
            "parsed_date": {
                "$ifNull": ["$p1", {"$ifNull": ["$p2", {"$ifNull": ["$p3", None]}]}]
            }
        }},
        {"$addFields": {
            "month_key": {"$cond": [
                {"$ne": ["$parsed_date", None]},
                {"$dateToString": {"format": "%Y-%m", "date": "$parsed_date"}},
                {"$substr": ["$created_at", 0, 7]}
            ]}
        }},
        {"$group": {"_id": "$month_key", "sales": {"$sum": "$total_amount"}, "count": {"$sum": 1}}},
        {"$sort": {"_id": -1}}, {"$limit": months},
    ]
    sales_by_month = await db.sales.aggregate(pipeline).to_list(months)
    svc_pipeline   = [{"$addFields":{"month_key":{"$substr":["$created_at",0,7]}}},{"$group":{"_id":"$month_key","service":{"$sum":"$grand_total"},"svc_count":{"$sum":1}}},{"$sort":{"_id":-1}},{"$limit":months}]
    svc_by_month   = await db.service_bills.aggregate(svc_pipeline).to_list(months)
    parts_pipeline = [{"$addFields":{"month_key":{"$substr":["$created_at",0,7]}}},{"$group":{"_id":"$month_key","parts":{"$sum":"$grand_total"},"parts_count":{"$sum":1}}},{"$sort":{"_id":-1}},{"$limit":months}]
    parts_by_month = await db.parts_sales.aggregate(parts_pipeline).to_list(months)
    return {"sales":oids(sales_by_month),"service":oids(svc_by_month),"parts":oids(parts_by_month)}

@api_router.get("/reports/daily-closing")
async def daily_closing_report(date: Optional[str] = Query(None), current_user=Depends(require_admin)):
    """Daily closing tally.
    Matches on parsed date (not raw string) — handles 'DD Mon YYYY', 'YYYY-MM-DD',
    'DD/MM/YYYY', and 'YYYY-MM-DDTHH:MM:SS' formats stored across collections.
    Amounts grouped by payment mode.
    """
    # Accept either "DD Mon YYYY" (from frontend) or "YYYY-MM-DD" (ISO)
    target_str = date or ist_now().strftime("%d %b %Y")
    # Parse target to a single canonical date object for range match
    target_dt = None
    for fmt in ("%d %b %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            target_dt = datetime.strptime(target_str.strip(), fmt)
            break
        except ValueError:
            continue
    if target_dt is None:
        raise HTTPException(status_code=400, detail=f"Invalid date: {target_str}")
    day_start = target_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end   = day_start + timedelta(days=1)

    async def get_totals(collection, date_field, amount_field):
        """Aggregate a collection: parse the date field defensively, match the target day,
        group by payment_mode, sum the amount field."""
        pipeline = [
            {"$addFields": {
                "p1": {"$dateFromString": {"dateString": f"${date_field}", "format": "%d %b %Y", "onError": None, "onNull": None}},
                "p2": {"$dateFromString": {"dateString": {"$substr": [f"${date_field}", 0, 10]}, "format": "%Y-%m-%d", "onError": None, "onNull": None}},
                "p3": {"$dateFromString": {"dateString": f"${date_field}", "format": "%d/%m/%Y", "onError": None, "onNull": None}},
            }},
            {"$addFields": {"parsed": {"$ifNull": ["$p1", {"$ifNull": ["$p2", "$p3"]}]}}},
            {"$match": {"parsed": {"$gte": day_start, "$lt": day_end}}},
            {"$group": {"_id": {"$toLower": "$payment_mode"}, "total": {"$sum": amount_field}}},
        ]
        return await db[collection].aggregate(pipeline).to_list(None)

    sales_r, service_r, parts_sales_r, parts_bills_r = await asyncio.gather(
        get_totals("sales",         "sale_date",   "$total_amount"),
        get_totals("service_bills", "bill_date",   "$grand_total"),
        get_totals("parts_sales",   "sale_date",   "$grand_total"),
        get_totals("parts_bills",   "created_at",  "$grand_total"),   # walk-in bills stamp created_at only
    )

    # Merge parts_sales + parts_bills under one "Parts" bucket per payment mode
    parts_r: dict = {}
    for item in parts_sales_r + parts_bills_r:
        m = item["_id"] or "unknown"
        parts_r[m] = parts_r.get(m, 0) + item["total"]
    parts_merged = [{"_id": m, "total": t} for m, t in parts_r.items()]

    summary = {}
    for source, data in [("Sales", sales_r), ("Service", service_r), ("Parts", parts_merged)]:
        for item in data:
            mode = (item["_id"] or "unknown").title()
            if mode not in summary:
                summary[mode] = {"total": 0, "Sales": 0, "Service": 0, "Parts": 0}
            summary[mode][source] += item["total"]
            summary[mode]["total"] += item["total"]

    result = [{"payment_mode": k, **v} for k, v in summary.items()]
    result.sort(key=lambda x: 0 if x["payment_mode"] == "Cash" else 1)
    return {"date": target_str, "breakdown": result, "grand_total": sum(r["total"] for r in result)}


@api_router.get("/reports/monthly-counts")
async def monthly_counts(months: int = Query(6, ge=1, le=24), current_user=Depends(verify_token)):
    """Monthly transaction counts — vehicle sales, service bills, parts bills + parts sales."""
    # Sales: parse sale_date (multi-format like GSTR1 does)
    sales_pipe = [
        {"$addFields": {
            "p1": {"$dateFromString": {"dateString": "$sale_date", "format": "%d %b %Y", "onError": None, "onNull": None}},
            "p2": {"$dateFromString": {"dateString": {"$substr": ["$sale_date", 0, 10]}, "format": "%Y-%m-%d", "onError": None, "onNull": None}},
            "p3": {"$dateFromString": {"dateString": "$sale_date", "format": "%d/%m/%Y", "onError": None, "onNull": None}},
        }},
        {"$addFields": {"parsed": {"$ifNull": ["$p1", {"$ifNull": ["$p2", "$p3"]}]}}},
        {"$addFields": {"month_key": {"$cond": [{"$ne": ["$parsed", None]},
            {"$dateToString": {"format": "%Y-%m", "date": "$parsed"}},
            {"$substr": ["$created_at", 0, 7]}]}}},
        {"$group": {"_id": "$month_key", "count": {"$sum": 1}}},
        {"$sort": {"_id": -1}}, {"$limit": months},
    ]
    # Service bills: use bill_date ("DD Mon YYYY") — same field daily-closing uses
    svc_pipe = [
        {"$addFields": {
            "p1": {"$dateFromString": {"dateString": "$bill_date", "format": "%d %b %Y", "onError": None, "onNull": None}},
        }},
        {"$addFields": {"month_key": {"$cond": [{"$ne": ["$p1", None]},
            {"$dateToString": {"format": "%Y-%m", "date": "$p1"}},
            {"$substr": ["$created_at", 0, 7]}]}}},
        {"$group": {"_id": "$month_key", "count": {"$sum": 1}}},
        {"$sort": {"_id": -1}}, {"$limit": months},
    ]
    # Parts bills (walk-in billing — primary source)
    parts_bills_pipe = [
        {"$addFields": {"month_key": {"$substr": ["$created_at", 0, 7]}}},
        {"$group": {"_id": "$month_key", "count": {"$sum": 1}}},
        {"$sort": {"_id": -1}}, {"$limit": months},
    ]
    # Parts sales (counter bills with stock deduction — secondary source)
    parts_sales_pipe = [
        {"$addFields": {"month_key": {"$substr": ["$created_at", 0, 7]}}},
        {"$group": {"_id": "$month_key", "count": {"$sum": 1}}},
        {"$sort": {"_id": -1}}, {"$limit": months},
    ]
    sales_r, svc_r, pb_r, ps_r = await asyncio.gather(
        db.sales.aggregate(sales_pipe).to_list(months),
        db.service_bills.aggregate(svc_pipe).to_list(months),
        db.parts_bills.aggregate(parts_bills_pipe).to_list(months),
        db.parts_sales.aggregate(parts_sales_pipe).to_list(months),
    )
    # Merge parts_bills + parts_sales counts per month
    parts_map: dict = {}
    for doc in pb_r:
        m = doc["_id"] or ""
        if m: parts_map[m] = parts_map.get(m, 0) + doc["count"]
    for doc in ps_r:
        m = doc["_id"] or ""
        if m: parts_map[m] = parts_map.get(m, 0) + doc["count"]
    parts_merged = sorted(
        [{"month": m, "count": c} for m, c in parts_map.items()],
        key=lambda x: x["month"], reverse=True
    )[:months]
    return {
        "sales":   [{"month": d["_id"], "count": d["count"]} for d in sales_r if d["_id"]],
        "service": [{"month": d["_id"], "count": d["count"]} for d in svc_r   if d["_id"]],
        "parts":   parts_merged,
    }

@api_router.post("/migrations/backfill-service-dates")
async def backfill_service_dates(current_user=Depends(require_admin)):
    """
    One-time migration: sets created_at = parsed check_in_date for imported service jobs
    where created_at is today (import artifact). Fixes Service Due calculations.
    """
    updated = 0; skipped = 0; errors = 0
    today_prefix = utcnow().strftime("%Y-%m-%dT")

    async for job in db.service_jobs.find(
        {"_imported": True, "check_in_date": {"$exists": True, "$ne": ""}},
        {"_id": 1, "check_in_date": 1, "created_at": 1}
    ):
        check_in = job.get("check_in_date", "")
        try:
            for fmt in ("%d/%m/%Y", "%d %b %Y", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(check_in, fmt); break
                except ValueError:
                    continue
            else:
                skipped += 1; continue
            await db.service_jobs.update_one(
                {"_id": job["_id"]},
                {"$set": {"created_at": parsed.isoformat()}}
            )
            updated += 1
        except Exception:
            errors += 1

    return {"updated": updated, "skipped": skipped, "errors": errors,
            "message": f"✅ {updated} service jobs backfilled with correct dates. Run once only."}


@api_router.post("/migrations/backfill-delivered-milestones")
async def backfill_delivered_milestones(
    include_pending_completed: bool = Query(False, description="If true, also include sales with status=completed"),
    current_user=Depends(require_admin),
):
    """
    One-time migration: sets all 5 milestones to True for every sale with
    status='delivered' (and optionally 'completed'). Idempotent — safe to
    re-run. Uses a single bulk update.
    """
    all_true = {k: True for k in SALE_MILESTONE_KEYS}
    status_filter = ["delivered"]
    if include_pending_completed:
        status_filter.append("completed")

    result = await db.sales.update_many(
        {"status": {"$in": status_filter}},
        {"$set": {"milestones": all_true}},
    )
    return {
        "matched":  result.matched_count,
        "modified": result.modified_count,
        "status_filter": status_filter,
        "message": f"✅ {result.modified_count} delivered sales marked with all 5 milestones complete.",
    }


@api_router.post("/migrations/backfill-sale-addresses")
async def backfill_sale_addresses(current_user=Depends(require_admin)):
    """
    One-time migration: copies customer.address → sale.customer_address
    for every sale that is missing the address field or has it blank.
    Also copies back into the customers collection if the customer has no address.
    """
    updated = 0
    skipped = 0
    no_address = 0

    async for sale in db.sales.find(
        {"$or": [{"customer_address": {"$exists": False}}, {"customer_address": ""}, {"customer_address": None}]},
        {"_id": 1, "customer_id": 1, "customer_mobile": 1}
    ):
        # Try by customer_id first, fall back to mobile
        customer = None
        if sale.get("customer_id"):
            try:
                customer = await db.customers.find_one({"_id": obj_id(sale["customer_id"])}, {"address": 1})
            except Exception:
                pass
        if not customer and sale.get("customer_mobile"):
            customer = await db.customers.find_one({"mobile": sale["customer_mobile"]}, {"address": 1})

        address = (customer or {}).get("address", "").strip() if customer else ""

        if address:
            await db.sales.update_one(
                {"_id": sale["_id"]},
                {"$set": {"customer_address": address}}
            )
            updated += 1
        else:
            no_address += 1

        await asyncio.sleep(0) if updated % 100 == 0 else None

    return {
        "updated": updated,
        "no_customer_address": no_address,
        "message": f"✅ {updated} sales updated with address. {no_address} customers have no address on file — import template with addresses to fill those."
    }


@api_router.get("/reports/brand-sales")
async def brand_sales_report(current_user=Depends(require_admin)):
    pipeline = [{"$group":{"_id":"$vehicle_brand","units":{"$sum":1},"revenue":{"$sum":"$total_amount"}}},{"$sort":{"units":-1}}]
    docs = await db.sales.aggregate(pipeline).to_list(None)
    return [{"brand":d["_id"],"units":d["units"],"revenue":d["revenue"]} for d in docs]

@api_router.get("/reports/top-parts")
async def top_parts_report(limit: int = Query(10), current_user=Depends(require_admin)):
    pipeline = [{"$unwind":"$items"},{"$group":{"_id":"$items.name","qty":{"$sum":"$items.qty"},"revenue":{"$sum":"$items.total"}}},{"$sort":{"qty":-1}},{"$limit":limit}]
    docs = await db.parts_sales.aggregate(pipeline).to_list(limit)
    return [{"name":d["_id"],"qty_sold":d["qty"],"revenue":round(d["revenue"],2)} for d in docs]

def _norm_key(s) -> str:
    return " ".join(str(s or "").upper().split())

def _top_sellers(sales: list, bills: list, limit: int) -> dict:
    """Best-selling bike models and the most-billed service items for one period.
    Spelling/case differences are merged; the labour line is reported on its own."""
    models: dict = {}
    for s in sales:
        key = _norm_key(s.get("vehicle_model"))   # ranked by model name
        if not key:
            continue
        m = models.setdefault(key, {"brand": _norm_key(s.get("vehicle_brand")), "model": (s.get("vehicle_model") or "").strip(), "units": 0, "revenue": 0.0})
        m["units"] += 1
        m["revenue"] += float(s.get("total_amount") or 0)

    items: dict = {}
    labour = {"bills": 0, "revenue": 0.0}
    for b in bills:
        seen = set()
        for it in b.get("items") or []:
            desc, pn = (it.get("description") or "").strip(), (it.get("part_number") or "").strip()
            if not pn and desc.lower() == "labour charges":
                labour["bills"] += 1
                labour["revenue"] += float(it.get("total") or 0)
                continue
            key = _norm_key(pn) or _norm_key(desc)
            if not key:
                continue
            row = items.setdefault(key, {"name": desc or pn, "part_number": pn, "qty": 0, "free_qty": 0, "revenue": 0.0, "bills": 0})
            qty = int(it.get("qty") or 0)
            row["qty"] += qty
            if it.get("complimentary"):
                row["free_qty"] += qty
            row["revenue"] += float(it.get("total") or 0)
            if key not in seen:
                row["bills"] += 1
                seen.add(key)

    def top(rows, *sort_keys):
        out = sorted(rows, key=lambda r: tuple(-r[k] for k in sort_keys))[:limit]
        for r in out:
            r["revenue"] = round(r["revenue"], 2)
        return out

    labour["revenue"] = round(labour["revenue"], 2)
    return {
        "sales_count":   len(sales),
        "service_bills": len(bills),
        "models":        top(models.values(), "units", "revenue"),
        "items":         top(items.values(), "qty", "revenue"),
        "labour":        labour,
    }

@api_router.get("/reports/top-sellers")
async def top_sellers_report(
    start: str = Query(..., description="YYYY-MM-DD"),
    end:   str = Query(..., description="YYYY-MM-DD, included"),
    limit: int = Query(10, ge=1, le=50),
    current_user=Depends(require_admin),
):
    """Top bike models sold and top service-bill items between two dates."""
    try:
        s = datetime.strptime(start, "%Y-%m-%d")
        e = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    if e <= s:
        raise HTTPException(status_code=400, detail="End date is before start date")
    sales, bills = await asyncio.gather(_gstr1_fetch_sales(s, e), _gstr1_fetch_service_bills(s, e))
    return _top_sellers(sales, bills, limit)


# ─── GSTR-1 Export (CA flat format) ────────────────────────────────────────────
# Single-sheet flat report matching accountant's template.
# 3 header rows + 34-col table + totals row.
# 18% GST inclusive back-calc, CGST+SGST split, DD/MM/YYYY dates.
# One row per bill (service_bills = SERVICES BILL, sales = SALES BILL).

_GSTR1_GST_RATE = 18.0  # Service + showroom charges

def _parse_month_range(month: str):
    """month = 'YYYY-MM' → (start_dt, end_dt_exclusive, label)."""
    from datetime import datetime as _dt
    y, m = month.split("-")
    y, m = int(y), int(m)
    start = _dt(y, m, 1)
    end   = _dt(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
    return start, end, month

def _split_gst_inclusive(total: float, rate: float):
    """Back-calc taxable + CGST + SGST from GST-inclusive total.
    Rounds to 2dp per invoice. Karnataka intra-state → equal CGST/SGST."""
    total = float(total or 0)
    taxable = round(total / (1 + rate / 100), 2)
    gst_total = round(total - taxable, 2)
    cgst = round(gst_total / 2, 2)
    sgst = round(gst_total - cgst, 2)
    return taxable, cgst, sgst

async def _gstr1_fetch_sales(start, end):
    """Sales in month. sale_date format varies — parse defensively."""
    pipeline = [
        {"$addFields": {
            "p1": {"$dateFromString": {"dateString": "$sale_date", "format": "%d %b %Y", "onError": None, "onNull": None}},
            "p2": {"$dateFromString": {"dateString": {"$substr": ["$sale_date", 0, 10]}, "format": "%Y-%m-%d", "onError": None, "onNull": None}},
            "p3": {"$dateFromString": {"dateString": "$sale_date", "format": "%d/%m/%Y", "onError": None, "onNull": None}},
        }},
        {"$addFields": {"parsed": {"$ifNull": ["$p1", {"$ifNull": ["$p2", "$p3"]}]}}},
        {"$match": {"parsed": {"$gte": start, "$lt": end}}},
        {"$sort": {"parsed": 1}},
    ]
    return await db.sales.aggregate(pipeline).to_list(20000)

async def _gstr1_fetch_service_bills(start, end):
    """Service bills in month. bill_date format 'DD Mon YYYY'."""
    pipeline = [
        {"$addFields": {
            "p1": {"$dateFromString": {"dateString": "$bill_date", "format": "%d %b %Y", "onError": None, "onNull": None}},
            "p2": {"$dateFromString": {"dateString": {"$substr": ["$created_at", 0, 10]}, "format": "%Y-%m-%d", "onError": None, "onNull": None}},
        }},
        {"$addFields": {"parsed": {"$ifNull": ["$p1", "$p2"]}}},
        {"$match": {"parsed": {"$gte": start, "$lt": end}}},
        {"$sort": {"parsed": 1}},
    ]
    return await db.service_bills.aggregate(pipeline).to_list(20000)

def _fmt_ddmmyyyy(parsed):
    """MongoDB Motor returns datetime for parsed date. Fallback to blank."""
    from datetime import datetime as _dt
    if isinstance(parsed, _dt):
        return parsed.strftime("%d/%m/%Y")
    return ""

def _customer_display(name, mobile):
    name = (name or "").strip()
    mob  = (mobile or "").strip()
    if mob and mob not in name:
        return f"{name} {mob}".strip()
    return name

def _build_gstr1_workbook(month_label, sales, service_bills, period_from, period_to):
    """Build CA's flat single-sheet GST report."""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from datetime import datetime as _dt

    wb = Workbook()
    ws = wb.active
    ws.title = "GST"

    title_font  = Font(name="Arial", bold=True, size=12)
    sub_font    = Font(name="Arial", size=10)
    header_font = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    header_fill = PatternFill("solid", fgColor="1F3864")
    body_font   = Font(name="Arial", size=9)
    center      = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin        = Side(border_style="thin", color="999999")
    box         = Border(left=thin, right=thin, top=thin, bottom=thin)

    # Header rows (3)
    ws.cell(row=1, column=1, value="M M MOTORS").font = title_font
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=34)
    ws.cell(row=2, column=1, value="Bengaluru Main Road, Beside Ruchi Bakery, Malur, 563130  Ph : 7026263123  GSTIN: 29CUJPM6814P1ZQ").font = sub_font
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=34)
    period_str = f"Sales Invoice       From {period_from.strftime('%d/%m/%Y')} To {(period_to - timedelta(days=1)).strftime('%d/%m/%Y')}"
    ws.cell(row=3, column=1, value=period_str).font = sub_font
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=34)

    # Column header row (row 4, matching CA template exactly)
    headers = [
        "Branch Name","FormatName","Date","Bill No.","Bill/Reff. Date","Bill Reff. No.",
        "Customer Name","Party GSTIN","Item","AdditionalDescription","Item Group",
        "Quantity","Free Quantity","Rate","Item MRP","Billed Qty","Billed UOM","Billed Rate",
        "TaxAmount","Unit","Gross Amount","Disc %","Discount Amount","Taxable Value",
        "Pre Tax","Post Tax","Round Off","Net Amount","SGST Amount","CGST Amount",
        "IGST Amount","HSN/SAC Code","Cess Amount","Bill Amount",
    ]
    for col_idx, h in enumerate(headers, 1):
        c = ws.cell(row=4, column=col_idx, value=h)
        c.font = header_font; c.fill = header_fill; c.alignment = center; c.border = box

    # Data rows
    rows = []  # list of (parsed_date, format_name, dict of values)

    # Service bills → SERVICES BILL
    for b in service_bills:
        total = float(b.get("net_total") or b.get("grand_total") or 0)
        if total <= 0:
            continue
        taxable, cgst, sgst = _split_gst_inclusive(total, _GSTR1_GST_RATE)
        tax_total = round(cgst + sgst, 2)
        bill_amt  = round(taxable + tax_total)  # CA rounds to whole rupee
        round_off = round(bill_amt - (taxable + tax_total), 2)
        rows.append({
            "parsed":       b.get("parsed"),
            "format_name":  "SERVICES BILL",
            "date":         _fmt_ddmmyyyy(b.get("parsed")),
            "bill_no":      b.get("bill_number", ""),
            "customer":     _customer_display(b.get("customer_name",""), b.get("customer_mobile","")),
            "gstin":        (b.get("customer_gstin","") or "").strip().upper(),
            "item":         "Service",
            "taxable":      taxable,
            "tax_total":    tax_total,
            "cgst":         cgst,
            "sgst":         sgst,
            "round_off":    round_off,
            "bill_amount":  bill_amt,
        })

    # Sales → SALES BILL
    for s in sales:
        total = float(s.get("total_amount") or s.get("sale_price") or 0)
        if total <= 0:
            continue
        taxable, cgst, sgst = _split_gst_inclusive(total, _GSTR1_GST_RATE)
        tax_total = round(cgst + sgst, 2)
        bill_amt  = round(taxable + tax_total)
        round_off = round(bill_amt - (taxable + tax_total), 2)
        rows.append({
            "parsed":       s.get("parsed"),
            "format_name":  "SALES BILL",
            "date":         _fmt_ddmmyyyy(s.get("parsed")),
            "bill_no":      s.get("invoice_number", ""),
            "customer":     _customer_display(s.get("customer_name",""), s.get("customer_mobile","")),
            "gstin":        (s.get("customer_gstin","") or "").strip().upper(),
            "item":         "Showroom Charges/CONSULTATION",
            "taxable":      taxable,
            "tax_total":    tax_total,
            "cgst":         cgst,
            "sgst":         sgst,
            "round_off":    round_off,
            "bill_amount":  bill_amt,
        })

    # Sort by parsed date then format
    rows.sort(key=lambda r: (r["parsed"] or _dt.min, r["format_name"]))

    r_idx = 5  # data starts row 5
    for r in rows:
        ws.cell(row=r_idx, column=1,  value="Main Location")
        ws.cell(row=r_idx, column=2,  value=r["format_name"])
        ws.cell(row=r_idx, column=3,  value=r["date"])
        ws.cell(row=r_idx, column=4,  value=r["bill_no"])
        ws.cell(row=r_idx, column=5,  value="")   # Bill/Reff. Date
        ws.cell(row=r_idx, column=6,  value="")   # Bill Reff. No.
        ws.cell(row=r_idx, column=7,  value=r["customer"])
        ws.cell(row=r_idx, column=8,  value=r["gstin"])
        ws.cell(row=r_idx, column=9,  value=r["item"])
        ws.cell(row=r_idx, column=10, value="")   # AdditionalDescription
        ws.cell(row=r_idx, column=11, value="Main Group")
        ws.cell(row=r_idx, column=12, value=1)      # Quantity
        ws.cell(row=r_idx, column=13, value=0)      # Free Quantity
        ws.cell(row=r_idx, column=14, value=r["taxable"])   # Rate
        ws.cell(row=r_idx, column=15, value=0.00)   # Item MRP
        ws.cell(row=r_idx, column=16, value=1)      # Billed Qty
        ws.cell(row=r_idx, column=17, value="Amt")  # Billed UOM
        ws.cell(row=r_idx, column=18, value=r["taxable"])   # Billed Rate
        ws.cell(row=r_idx, column=19, value=r["tax_total"]) # TaxAmount
        ws.cell(row=r_idx, column=20, value="Amt")  # Unit
        ws.cell(row=r_idx, column=21, value=r["taxable"])   # Gross Amount
        ws.cell(row=r_idx, column=22, value=0)      # Disc %
        ws.cell(row=r_idx, column=23, value=0.00)   # Discount Amount
        ws.cell(row=r_idx, column=24, value=r["taxable"])   # Taxable Value
        ws.cell(row=r_idx, column=25, value=0)      # Pre Tax
        ws.cell(row=r_idx, column=26, value=0)      # Post Tax
        ws.cell(row=r_idx, column=27, value=r["round_off"]) # Round Off
        ws.cell(row=r_idx, column=28, value=r["taxable"])   # Net Amount
        ws.cell(row=r_idx, column=29, value=r["sgst"])      # SGST Amount
        ws.cell(row=r_idx, column=30, value=r["cgst"])      # CGST Amount
        ws.cell(row=r_idx, column=31, value=0)      # IGST Amount
        ws.cell(row=r_idx, column=32, value="")     # HSN/SAC Code
        ws.cell(row=r_idx, column=33, value=0)      # Cess Amount
        ws.cell(row=r_idx, column=34, value=r["bill_amount"])
        for col_idx in range(1, 35):
            c = ws.cell(row=r_idx, column=col_idx)
            c.font = body_font
            c.border = box
        r_idx += 1

    # Totals row (formulas, not hardcoded)
    if rows:
        from openpyxl.utils import get_column_letter as _gcl
        first_data_row = 5
        last_data_row  = r_idx - 1
        totals_row     = r_idx
        # Only sum numeric columns per CA template: Quantity(12), Free Qty(13),
        # TaxAmount(19), Gross Amount(21), Disc Amt(23), Taxable(24), Pre Tax(25),
        # Post Tax(26), Round Off(27), Net Amount(28), SGST(29), CGST(30),
        # IGST(31), Cess(33), Bill Amount(34)
        sum_cols = [12, 13, 19, 21, 23, 24, 25, 26, 27, 28, 29, 30, 31, 33, 34]
        for col_idx in sum_cols:
            col_letter = _gcl(col_idx)
            ws.cell(row=totals_row, column=col_idx,
                    value=f"=SUM({col_letter}{first_data_row}:{col_letter}{last_data_row})")
        for col_idx in range(1, 35):
            c = ws.cell(row=totals_row, column=col_idx)
            c.font = Font(name="Arial", size=9, bold=True)
            c.border = box
            c.fill = PatternFill("solid", fgColor="FFF2CC")

    # Column widths
    from openpyxl.utils import get_column_letter as _gcl2
    widths = {
        1:14, 2:14, 3:12, 4:12, 5:12, 6:12, 7:26, 8:16, 9:22, 10:16, 11:14,
        12:10, 13:10, 14:12, 15:10, 16:10, 17:10, 18:12, 19:12, 20:8,
        21:14, 22:8, 23:12, 24:14, 25:10, 26:10, 27:10, 28:14, 29:12, 30:12,
        31:12, 32:14, 33:10, 34:14,
    }
    for col_idx, w in widths.items():
        ws.column_dimensions[_gcl2(col_idx)].width = w

    ws.freeze_panes = "A5"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf

@api_router.get("/reports/gstr1")
async def gstr1_export(
    month: str = Query(..., regex=r"^\d{4}-\d{2}$"),
    current_user=Depends(require_admin),
):
    """GST monthly export in CA's flat format (YYYY-MM). Returns xlsx blob.
    Single sheet: Business header + 34-col flat table + totals row.
    18% inclusive GST back-calc, CGST/SGST equal split, Karnataka intra-state."""
    try:
        start, end, label = _parse_month_range(month)
    except Exception:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")
    sales, service_bills = await asyncio.gather(
        _gstr1_fetch_sales(start, end),
        _gstr1_fetch_service_bills(start, end),
    )
    buf = _build_gstr1_workbook(label, sales, service_bills, start, end)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="GST_{label}.xlsx"'},
    )


@api_router.get("/reports/complimentary")
async def complimentary_report(
    date_from: Optional[str] = Query(None),
    date_to:   Optional[str] = Query(None),
    current_user=Depends(verify_token),
):
    """Complimentary (free) items given across service_bills and parts_bills.
    Groups by part_number / name. Shows MRP value forgone."""
    match: dict = {"items.complimentary": True}
    if date_from or date_to:
        rng: dict = {}
        if date_from: rng["$gte"] = date_from
        if date_to:   rng["$lte"] = date_to
        match["created_at"] = rng

    pipeline = [
        {"$match": match},
        {"$unwind": "$items"},
        {"$match": {"items.complimentary": True}},
        {"$group": {
            "_id": {"pn": "$items.part_number", "name": {"$ifNull": ["$items.name", "$items.description"]}},
            "qty":       {"$sum": "$items.qty"},
            "mrp_value": {"$sum": {"$multiply": [{"$ifNull": ["$items.mrp", 0]}, "$items.qty"]}},
            "occurrences": {"$sum": 1},
        }},
        {"$sort": {"qty": -1}},
    ]
    sb, pb = await asyncio.gather(
        db.service_bills.aggregate(pipeline).to_list(500),
        db.parts_bills.aggregate(pipeline).to_list(500),
    )
    merged: dict = {}
    for d in (sb + pb):
        key = d["_id"].get("pn") or d["_id"].get("name")
        row = merged.setdefault(key, {
            "part_number": d["_id"].get("pn",""),
            "name":        d["_id"].get("name",""),
            "qty": 0, "mrp_value": 0.0, "occurrences": 0,
        })
        row["qty"]         += d["qty"]
        row["mrp_value"]   += d["mrp_value"]
        row["occurrences"] += d["occurrences"]
    result = sorted(merged.values(), key=lambda r: r["qty"], reverse=True)
    return {"items": result, "total_value": round(sum(r["mrp_value"] for r in result), 2)}
