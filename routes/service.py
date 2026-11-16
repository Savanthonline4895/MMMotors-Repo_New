"""API routes: service (split out of server.py; code moved verbatim)."""
import asyncio
import secrets
import re
from datetime import datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException, Query
from fastapi.responses import JSONResponse
import os
from database import verify_token, require_admin, next_sequence, _sync_counter, oid, oids, obj_id, utcnow, ist_now
from models import FeedbackSubmit, ServiceJobCreate, ServiceJobUpdate
from routers import api_router

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

# ═══════════════════════════════════════════════════════════════════════════════
#  SERVICE JOBS
# ═══════════════════════════════════════════════════════════════════════════════

@api_router.get("/service")
async def list_service_jobs(
    status:      Optional[str] = Query(None),
    customer_id: Optional[str] = Query(None),
    technician:  Optional[str] = Query(None),
    search:      Optional[str] = Query(None),
    limit:       int           = Query(200, ge=1, le=5000),
    current_user=Depends(verify_token),
):
    query: dict = {}
    if status:      query["status"]      = status
    if customer_id: query["customer_id"] = customer_id
    if technician:  query["technician"]  = technician
    if search:
        s = re.escape(search)
        query["$or"] = [
            {"job_number":     {"$regex": s, "$options": "i"}},
            {"customer_name":  {"$regex": s, "$options": "i"}},
            {"vehicle_number": {"$regex": s, "$options": "i"}},
            {"model":          {"$regex": s, "$options": "i"}},
            {"complaint":      {"$regex": s, "$options": "i"}},
        ]
    docs  = await db.service_jobs.find(query).sort("created_at",-1).limit(limit).to_list(limit)
    total = await db.service_jobs.count_documents(query)
    return JSONResponse(content=oids(docs), headers={"X-Total-Count": str(total)})

def _photo_ids(ids) -> list:
    """Check-in photo ids from the client: GridFS ObjectId strings only, de-duplicated, max 8."""
    out = []
    for x in ids or []:
        x = str(x).strip()
        if not re.fullmatch(r"[0-9a-f]{24}", x):
            raise HTTPException(status_code=400, detail="Invalid photo id")
        if x not in out:
            out.append(x)
    return out[:8]


@api_router.post("/service", status_code=201)
async def create_service_job(body: ServiceJobCreate, current_user=Depends(verify_token)):
    customer = await db.customers.find_one({"_id": obj_id(body.customer_id)})
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    job_no   = await next_sequence("job")
    check_in = body.check_in_date or ist_now().strftime("%d %b %Y")
    veh_no   = body.vehicle_number.strip().upper() if body.vehicle_number else ""
    chassis  = body.chassis_number.strip().upper() if body.chassis_number else ""
    doc = {
        "job_number":         job_no,
        "customer_id":        body.customer_id,
        "customer_name":      customer["name"],
        "customer_mobile":    customer.get("mobile",""),
        "customer_address":   customer.get("address",""),
        "vehicle_number":     veh_no,
        "chassis_number":     chassis,
        "brand":              body.brand.upper(),
        "model":              body.model,
        "variant":            body.variant or "",
        "odometer_km":        body.odometer_km or 0,
        "odometer_out":       0,
        "complaint":          body.complaint,
        "advisor_id":         body.advisor_id or "",
        "technician":         body.technician or "",
        "check_in_date":      check_in,
        "estimated_delivery": body.estimated_delivery or "",
        "delivery_date":      "",
        "status":             "pending",
        "notes":              body.notes or "",
        "service_type":       body.service_type or "",
        "checkin_photo_ids":  _photo_ids(body.checkin_photo_ids),
        "fuel_level":         body.fuel_level or "",
        "damage_notes":       (body.damage_notes or "").strip(),
        "created_at":         utcnow().isoformat(),
    }
    result = await db.service_jobs.insert_one(doc)
    doc["id"] = str(result.inserted_id); doc.pop("_id", None)

    # Option B: save vehicle to records if requested (service-only customers)
    if getattr(body, "save_vehicle", False):
        # Only create if not already in vehicles collection
        dedup_query = {"chassis_number": chassis} if chassis else {"vehicle_number": veh_no} if veh_no else None
        existing_veh = await db.vehicles.find_one(dedup_query) if dedup_query else None
        if not existing_veh:
            await db.vehicles.insert_one({
                "brand":            body.brand.upper(),
                "model":            body.model,
                "variant":          body.variant or "",
                "chassis_number":   chassis,
                "vehicle_number":   veh_no,
                "engine_number":    "",
                "color":            "",
                "key_number":       "",
                "type":             "used",
                "status":           "in_service",
                "customer_id":      body.customer_id,
                "customer_name":    customer["name"],
                "customer_mobile":  customer.get("mobile",""),
                "inbound_date":     check_in,
                "inbound_location": "Service",
                "return_date":      "",
                "returned_location":"",
                "created_at":       utcnow().isoformat(),
                "_source":          "service",
            })

    return doc


# ── Service Due ────────────────────────────────────────────────────────────────
# Module-level TTL cache — invalidated on sales/service_jobs writes
_SERVICE_DUE_CACHE: dict = {}   # key: days (int) → {"ts": datetime, "data": list}
_SERVICE_DUE_TTL_SEC = 60

# Job-card service types. Only the numbered ones advance the service counter
# and reset the 90-day clock; REPAIR is invisible to the schedule.
REPAIR_SERVICE_TYPE = "Repair / Walk-in"
SCHEDULED_SERVICE_TYPES = ["1st Service", "2nd Service", "3rd Service",
                           "4th Service", "5th Service", "6th+ Service"]

def is_repair_job(service_type) -> bool:
    """True if this job should be ignored by the service schedule.
    Legacy rows (None / "") are treated as scheduled so existing history and
    counters are unchanged by the introduction of this field."""
    return (service_type or "").strip() == REPAIR_SERVICE_TYPE

def _invalidate_service_due_cache():
    _SERVICE_DUE_CACHE.clear()

@api_router.get("/service/due")
async def service_due(
    days: int               = Query(30, ge=1, le=365),  # lookahead window
    first_service_days: int = Query(30, ge=1, le=90),   # kept for API compat, ignored — hardcoded 30d
    current_user=Depends(verify_token),
):
    """
    Service schedule:
      Sold vehicles:
        1st service  = sale_date + 30 days
        2nd service  = 1st service date + 90 days
        3rd+ service = last service date + 90 days
      Service-only vehicles:
        next service = last service date + 90 days

    Returns vehicles whose next_due_date <= today + lookahead (days param).
    Cached in-memory for 60s; invalidated on sales/service_jobs write.
    """
    # Cache lookup
    cached = _SERVICE_DUE_CACHE.get(days)
    if cached and (utcnow() - cached["ts"]).total_seconds() < _SERVICE_DUE_TTL_SEC:
        return cached["data"]

    FIRST_SVC_INTERVAL  = 30   # days after sale for 1st service
    REPEAT_SVC_INTERVAL = 90   # days between 2nd+ services

    now         = utcnow()
    lookahead   = now + timedelta(days=days)

    def parse_date(s):
        for fmt in ("%d %b %Y", "%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
            try: return datetime.strptime(s[:len(fmt)+2].strip(), fmt)
            except (ValueError, TypeError): continue
        return None

    def ordinal(n):
        return {1:"1st",2:"2nd",3:"3rd"}.get(n, f"{n}th")

    # ── 1. Sales map: vehicle_number → most-recent sale ─────────────────────
    sales_docs = await db.sales.find(
        {}, {"vehicle_number":1,"sale_date":1,"customer_name":1,
             "customer_mobile":1,"vehicle_brand":1,"vehicle_model":1,"created_at":1}
    ).to_list(20000)

    # Cutoff from env var (YYYY-MM-DD), fallback 2026-05-01
    _cutoff_str = os.getenv("SERVICE_DUE_CUTOFF", "2026-05-01").strip()
    try:
        SOLD_SINCE = datetime.strptime(_cutoff_str, "%Y-%m-%d")
    except ValueError:
        SOLD_SINCE = datetime(2026, 5, 1)

    sales_map = {}
    for s in sales_docs:
        vn = (s.get("vehicle_number") or "").upper().strip()
        if not vn:
            continue
        dt = parse_date(s.get("sale_date","")) or parse_date(s.get("created_at",""))
        # Skip vehicles sold before May 1, 2026
        if not dt or dt < SOLD_SINCE:
            continue
        existing = sales_map.get(vn)
        if not existing or (dt and (not existing["dt"] or dt > existing["dt"])):
            sales_map[vn] = {
                "dt":     dt,
                "str":    s.get("sale_date",""),
                "name":   s.get("customer_name",""),
                "mobile": s.get("customer_mobile",""),
                "brand":  s.get("vehicle_brand",""),
                "model":  s.get("vehicle_model",""),
            }

    # ── 2. Service history per vehicle (oldest-first for counting) ───────────
    # Only SCHEDULED services advance the counter and reset the 90-day clock.
    # Repair / walk-in visits are recorded but invisible to the schedule, so a
    # headlight bulb in August can't bury a service that was due in June.
    # Legacy rows (no service_type) count as scheduled — history stays as-is.
    oldest_pipeline = [
        {"$match": {"status": "delivered"}},
        {"$sort": {"created_at": 1}},          # oldest first
        {"$group": {
            "_id":   "$vehicle_number",
            "jobs":  {"$push": {
                "job_id":   "$_id", "job_number": "$job_number",
                "created_at": "$created_at", "check_in_date": "$check_in_date",
                "customer_name": "$customer_name", "customer_mobile": "$customer_mobile",
                "brand": "$brand", "model": "$model", "complaint": "$complaint",
                "service_type": "$service_type",
            }},
        }},
    ]
    grouped = await db.service_jobs.aggregate(oldest_pipeline).to_list(10000)
    service_map = {}   # scheduled services only — drives the counter + clock
    alljob_map  = {}   # every delivered job — customer-info fallback only
    for g in grouped:
        vn = (g["_id"] or "").upper().strip()
        if not vn:
            continue
        alljob_map[vn]  = g["jobs"]
        service_map[vn] = [j for j in g["jobs"] if not is_repair_job(j.get("service_type"))]

    # ── 2b. Vehicles with an ACTIVE job (pending/in_progress/ready) ─────────
    # Active SCHEDULED service  → hide the vehicle; that service is being done.
    # Active REPAIR             → keep it listed with an in_workshop flag, so
    #                             staff can upsell the due service before the
    #                             customer leaves.
    active_docs = await db.service_jobs.find(
        {"status": {"$in": ["pending", "in_progress", "ready"]}},
        {"vehicle_number": 1, "service_type": 1, "job_number": 1, "status": 1},
    ).to_list(10000)
    active_service_vns  = set()   # hide these
    active_repair_vns   = {}      # vn → {job_number, status} — badge these
    for d in active_docs:
        vn = (d.get("vehicle_number") or "").upper().strip()
        if not vn:
            continue
        if is_repair_job(d.get("service_type")):
            active_repair_vns.setdefault(vn, {
                "job_number": d.get("job_number", ""),
                "status":     d.get("status", ""),
            })
        else:
            active_service_vns.add(vn)

    # ── 3. Compute next_due for every known vehicle ──────────────────────────
    result = []
    for vn in set(sales_map) | set(service_map) | set(alljob_map):
        # A scheduled service is already in progress — don't chase it.
        if vn in active_service_vns:
            continue
        is_sold     = vn in sales_map
        jobs        = service_map.get(vn, [])   # oldest → newest
        job_count   = len(jobs)
        last_job    = jobs[-1] if jobs else None
        first_job   = jobs[0]  if jobs else None

        last_svc_dt  = parse_date(last_job["created_at"])  if last_job  else None
        first_svc_dt = parse_date(first_job["created_at"]) if first_job else None

        # ── Determine next due date ──────────────────────────────────────────
        if is_sold:
            sale   = sales_map[vn]
            sale_dt = sale["dt"]

            if job_count == 0:
                # Never serviced — 1st service 30d after sale
                if not sale_dt: continue
                next_due   = sale_dt + timedelta(days=FIRST_SVC_INTERVAL)
                next_label = "1st Service"
                ref_date   = sale["str"]
                ref_label  = "Delivery date"
                source     = "sale"
            elif job_count == 1:
                # 1st done — 2nd service 90d after 1st service
                if not first_svc_dt: continue
                next_due   = first_svc_dt + timedelta(days=REPEAT_SVC_INTERVAL)
                next_label = "2nd Service"
                ref_date   = last_job.get("check_in_date","")
                ref_label  = last_job.get("complaint","")
                source     = "sale"
            else:
                # 2nd+ done — 90d from last service
                if not last_svc_dt: continue
                next_due   = last_svc_dt + timedelta(days=REPEAT_SVC_INTERVAL)
                next_label = f"{ordinal(job_count + 1)} Service"
                ref_date   = last_job.get("check_in_date","")
                ref_label  = last_job.get("complaint","")
                source     = "sale"
        else:
            # Service-only vehicle. Normally anchored on the last scheduled
            # service. If this vehicle has only ever had repair visits, fall
            # back to the last repair date so it stays tracked instead of
            # dropping off the list entirely.
            anchor_dt = last_svc_dt
            if not anchor_dt:
                repair_jobs = alljob_map.get(vn, [])
                anchor_dt = parse_date(repair_jobs[-1]["created_at"]) if repair_jobs else None
            if not anchor_dt: continue
            next_due   = anchor_dt + timedelta(days=REPEAT_SVC_INTERVAL)
            next_label = f"{ordinal(job_count + 1)} Service"
            ref_date   = (last_job or {}).get("check_in_date","")
            ref_label  = (last_job or {}).get("complaint","")
            source     = "service"

        # Skip if not due within lookahead window
        if next_due > lookahead:
            continue

        due_in_days = (next_due - now).days   # negative = overdue
        days_since  = (now - last_svc_dt).days if last_svc_dt else None
        urgency     = "overdue" if due_in_days < 0 else "due_soon"

        # Customer / vehicle info — prefer the most recent job of ANY kind
        # (a repair visit carries fresher contact details than an old service).
        info      = sales_map.get(vn, {})
        info_job  = (alljob_map.get(vn) or [None])[-1] or last_job
        if info_job:
            cname   = info_job.get("customer_name")   or info.get("name","")
            cmobile = info_job.get("customer_mobile") or info.get("mobile","")
            brand   = info_job.get("brand")           or info.get("brand","")
            model   = info_job.get("model")           or info.get("model","")
            jnum    = info_job.get("job_number","")
            jid     = str(info_job.get("job_id",""))
        else:
            cname   = info.get("name","")
            cmobile = info.get("mobile","")
            brand   = info.get("brand","")
            model   = info.get("model","")
            jnum    = ""
            jid     = ""

        workshop = active_repair_vns.get(vn)
        result.append({
            "id":               jid,
            "job_number":       jnum,
            "vehicle_number":   vn,
            "customer_name":    cname,
            "customer_mobile":  cmobile,
            "brand":            brand,
            "model":            model,
            "check_in_date":    ref_date,
            "complaint":        ref_label,
            "created_at":       last_svc_dt.isoformat() if last_svc_dt else "",
            "days_since":       days_since,
            "due_in_days":      due_in_days,
            "next_due_date":    next_due.strftime("%d %b %Y"),
            "service_count":    job_count,
            "service_type":     next_label,
            "urgency":          urgency,
            "source":           source,
            "vehicle_type":     "sold" if is_sold else "service_only",
            "in_workshop":          bool(workshop),
            "workshop_job_number":  (workshop or {}).get("job_number", ""),
            "workshop_status":      (workshop or {}).get("status", ""),
        })

    # In-workshop first (the upsell window closes when the customer leaves),
    # then most overdue, then soonest due.
    result.sort(key=lambda x: (not x["in_workshop"], x["due_in_days"]))
    trimmed = result[:500]
    _SERVICE_DUE_CACHE[days] = {"ts": utcnow(), "data": trimmed}
    return trimmed

@api_router.get("/service/schedule/{vehicle_number}")
async def vehicle_service_schedule(vehicle_number: str, current_user=Depends(verify_token)):
    """Next scheduled service for one vehicle — powers the job-card hint so
    staff can see whether this visit should be logged as a scheduled service
    or a repair, instead of guessing."""
    vn = (vehicle_number or "").upper().strip()
    if not vn:
        return {"found": False}

    FIRST_SVC_INTERVAL, REPEAT_SVC_INTERVAL = 30, 90
    now = utcnow()

    def parse_date(s):
        for fmt in ("%d %b %Y", "%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
            try: return datetime.strptime(s[:len(fmt)+2].strip(), fmt)
            except (ValueError, TypeError): continue
        return None

    def ordinal(n):
        return {1:"1st",2:"2nd",3:"3rd"}.get(n, f"{n}th")

    jobs = await db.service_jobs.find(
        {"vehicle_number": vn, "status": "delivered"},
        {"created_at": 1, "service_type": 1},
    ).sort("created_at", 1).to_list(500)
    scheduled = [j for j in jobs if not is_repair_job(j.get("service_type"))]
    count = len(scheduled)

    anchor, basis = None, ""
    if count:
        anchor = parse_date(scheduled[-1].get("created_at", ""))
        basis  = "last service"
        interval = REPEAT_SVC_INTERVAL
    else:
        sale = await db.sales.find_one({"vehicle_number": vn}, sort=[("created_at", -1)])
        if sale:
            anchor = parse_date(sale.get("sale_date", "")) or parse_date(sale.get("created_at", ""))
            basis  = "sale date"
            interval = FIRST_SVC_INTERVAL
    if not anchor:
        return {"found": False, "suggested_type": REPAIR_SERVICE_TYPE}

    next_due = anchor + timedelta(days=interval)
    days_out = (next_due - now).days
    label    = f"{ordinal(count + 1)} Service"
    # Suggest logging this visit as the scheduled service only when it's
    # genuinely near (or past) due; otherwise it's a repair.
    suggested = label if days_out <= 15 else REPAIR_SERVICE_TYPE

    return {
        "found":            True,
        "vehicle_number":   vn,
        "services_done":    count,
        "next_service":     label,
        "next_due_date":    next_due.strftime("%d %b %Y"),
        "days_until_due":   days_out,
        "basis":            basis,
        "suggested_type":   suggested,
    }

@api_router.post("/service/due/{vehicle_number}/notified")
async def mark_notified(vehicle_number: str, current_user=Depends(verify_token)):
    """Mark a vehicle as notified — stores timestamp so you know who was contacted."""
    key = vehicle_number.upper().strip()
    await db.service_notifications.update_one(
        {"vehicle_number": key},
        {"$set": {
            "vehicle_number":  key,
            "notified_at":     utcnow().isoformat(),
            "notified_by":     current_user.get("name", ""),
        }},
        upsert=True,
    )
    return {"message": "Marked as notified"}

@api_router.get("/service/due/notifications")
async def get_notifications(current_user=Depends(verify_token)):
    """Return vehicle_number → last notified_at map."""
    docs = await db.service_notifications.find({}).to_list(2000)
    return {d["vehicle_number"]: d["notified_at"] for d in docs if d.get("vehicle_number")}


@api_router.get("/service/stats")
async def service_stats(current_user=Depends(verify_token)):
    pending, in_progress, ready, delivered, fb = await asyncio.gather(
        db.service_jobs.count_documents({"status":"pending"}),
        db.service_jobs.count_documents({"status":"in_progress"}),
        db.service_jobs.count_documents({"status":"ready"}),
        db.service_jobs.count_documents({"status":"delivered"}),
        db.service_jobs.aggregate([
            {"$match": {"feedback_rating": {"$exists": True}}},
            {"$group": {"_id": None, "avg": {"$avg": "$feedback_rating"}, "count": {"$sum": 1}}},
        ]).to_list(1),
    )
    fb = fb[0] if fb else {"avg": None, "count": 0}
    return {"pending":pending,"in_progress":in_progress,"ready":ready,"delivered":delivered,"total_active":pending+in_progress+ready,
            "feedback_avg": round(fb["avg"], 1) if fb["avg"] is not None else None, "feedback_count": fb["count"]}

@api_router.get("/service/{job_id}")
async def get_service_job(job_id: str, current_user=Depends(verify_token)):
    doc = await db.service_jobs.find_one({"_id": obj_id(job_id)})
    if not doc:
        doc = await db.service_jobs.find_one({"job_number": job_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Service job not found")
    return oid(doc)

@api_router.put("/service/{job_id}")
async def update_service_job(job_id: str, body: ServiceJobUpdate, current_user=Depends(verify_token)):
    update = {k: v for k, v in body.dict().items() if v is not None}
    if "checkin_photo_ids" in update:
        update["checkin_photo_ids"] = _photo_ids(update["checkin_photo_ids"])
    if "status" in update and update["status"] == "delivered":
        update.setdefault("delivery_date", ist_now().strftime("%d %b %Y"))
    await db.service_jobs.update_one({"_id": obj_id(job_id)}, {"$set": update})
    if update.get("status") == "delivered":
        await _ensure_feedback_token(job_id)
    return oid(await db.service_jobs.find_one({"_id": obj_id(job_id)}))

# ── Customer feedback ─────────────────────────────────────────────────────────
# Each delivered job gets an unguessable one-time token; the customer opens
# /feedback/<token> on the frontend without logging in.
async def _ensure_feedback_token(job_id: str) -> str:
    # $exists filter keeps an already-sent link valid if this runs twice
    await db.service_jobs.update_one(
        {"_id": obj_id(job_id), "feedback_token": {"$exists": False}},
        {"$set": {"feedback_token": secrets.token_urlsafe(24)}},
    )
    job = await db.service_jobs.find_one({"_id": obj_id(job_id)}, {"feedback_token": 1})
    if not job:
        raise HTTPException(status_code=404, detail="Service job not found")
    return job["feedback_token"]

@api_router.post("/service/{job_id}/feedback-link")
async def service_feedback_link(job_id: str, current_user=Depends(verify_token)):
    return {"token": await _ensure_feedback_token(job_id)}

@api_router.get("/feedback/{token}")
async def get_feedback_form(token: str):
    """Public — returns only what the form shows, never the customer's mobile or address."""
    job = await db.service_jobs.find_one({"feedback_token": token})
    if not job:
        raise HTTPException(status_code=404, detail="Feedback link not found")
    return {
        "customer_name":  (job.get("customer_name") or "").split(" ")[0],
        "vehicle":        f'{job.get("brand","")} {job.get("model","")}'.strip(),
        "vehicle_number": job.get("vehicle_number",""),
        "submitted":      "feedback_rating" in job,
    }

@api_router.post("/feedback/{token}")
async def submit_feedback(token: str, body: FeedbackSubmit):
    """Public — one submission per token."""
    res = await db.service_jobs.update_one(
        {"feedback_token": token, "feedback_rating": {"$exists": False}},
        {"$set": {"feedback_rating": body.rating, "feedback_comment": (body.comment or "").strip(),
                  "feedback_at": utcnow().isoformat()}},
    )
    if res.matched_count == 0:
        if await db.service_jobs.count_documents({"feedback_token": token}, limit=1):
            raise HTTPException(status_code=409, detail="Feedback already submitted")
        raise HTTPException(status_code=404, detail="Feedback link not found")
    return {"message": "Thank you!"}

@api_router.delete("/service/{job_id}")
async def delete_service_job(job_id: str, current_user=Depends(require_admin)):
    result = await db.service_jobs.delete_one({"_id": obj_id(job_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Job not found")
    await db.service_bills.delete_many({"job_id": job_id})
    await _sync_counter("job", "service_jobs", "job_number")
    return {"message": "Deleted"}
