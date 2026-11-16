"""API routes: followups (split out of server.py; code moved verbatim)."""
import re
from datetime import datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from database import BRANDS, verify_token, require_admin, oid, oids, obj_id, utcnow, ist_now
from routes.auth import _client_ip
from models import ServiceJobCreate
from routers import api_router
from routes.service import REPAIR_SERVICE_TYPE, SCHEDULED_SERVICE_TYPES, create_service_job

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

# ═══════════════════════════════════════════════════════════════════════════════
#  FOLLOW-UPS — enquiries, insurance renewals, birthday & festival greetings
# ═══════════════════════════════════════════════════════════════════════════════

_YMD = r"^(\d{4}-\d{2}-\d{2})?$"

def _today_ist():
    """The shop's calendar date (the server runs on UTC)."""
    return ist_now().date()

def _parse_any_date(s):
    """Sale dates are stored as '26 Aug 2026', '2026-08-26' or '26/08/2026'."""
    s = str(s or "").strip()
    for fmt, n in (("%Y-%m-%d", 10), ("%d %b %Y", None), ("%d/%m/%Y", None)):
        try:
            return datetime.strptime(s[:n] if n else s, fmt).date()
        except ValueError:
            pass
    return None

def _add_year(d):
    try:
        return d.replace(year=d.year + 1)
    except ValueError:          # 29 Feb
        return d.replace(year=d.year + 1, day=28)

def _mobile10(s) -> str:
    digits = re.sub(r"\D", "", str(s or ""))
    return digits[-10:]

class EnquiryIn(BaseModel):
    name:           str             = Field(..., min_length=1, max_length=120)
    mobile:         str             = Field(..., min_length=10, max_length=20)
    brand:          Optional[str]   = Field("", max_length=40)
    model:          Optional[str]   = Field("", max_length=80)
    budget:         Optional[float] = Field(None, ge=0)
    finance:        Optional[bool]  = False
    source:         Optional[str]   = Field("Walk-in", max_length=40)
    notes:          Optional[str]   = Field("", max_length=2000)
    follow_up_date: Optional[str]   = Field("", pattern=_YMD)

class EnquiryUpdate(BaseModel):
    name:           Optional[str]   = Field(None, min_length=1, max_length=120)
    mobile:         Optional[str]   = Field(None, min_length=10, max_length=20)
    brand:          Optional[str]   = Field(None, max_length=40)
    model:          Optional[str]   = Field(None, max_length=80)
    budget:         Optional[float] = Field(None, ge=0)
    finance:        Optional[bool]  = None
    source:         Optional[str]   = Field(None, max_length=40)
    notes:          Optional[str]   = Field(None, max_length=2000)
    follow_up_date: Optional[str]   = Field(None, pattern=_YMD)
    status:         Optional[str]   = Field(None, pattern=r"^(open|converted|lost)$")
    lost_reason:    Optional[str]   = Field(None, max_length=200)

@api_router.get("/followups/enquiries")
async def list_enquiries(
    status: Optional[str] = Query(None, description="open | converted | lost; omit for all"),
    due:    bool          = Query(False, description="only open enquiries to call today or earlier"),
    search: Optional[str] = Query(None),
    current_user=Depends(verify_token),
):
    query: dict = {}
    if due:
        query = {"status": "open", "follow_up_date": {"$lte": _today_ist().isoformat()}}
    elif status:
        query["status"] = status
    if search:
        s = re.escape(search.strip())
        query["$or"] = [{"name": {"$regex": s, "$options": "i"}}, {"mobile": {"$regex": s}},
                        {"model": {"$regex": s, "$options": "i"}}]
    docs = await db.enquiries.find(query).sort([("follow_up_date", 1), ("created_at", -1)]).to_list(2000)
    return oids(docs)

@api_router.post("/followups/enquiries", status_code=201)
async def create_enquiry(body: EnquiryIn, current_user=Depends(verify_token)):
    doc = body.dict()
    doc["mobile"] = _mobile10(doc["mobile"])
    if len(doc["mobile"]) != 10:
        raise HTTPException(status_code=400, detail="Enter a 10-digit mobile number")
    # No date given → call back tomorrow, so it never silently drops off the list
    doc["follow_up_date"] = doc["follow_up_date"] or (_today_ist() + timedelta(days=1)).isoformat()
    doc.update({"status": "open", "created_at": utcnow().isoformat(),
                "created_by": current_user.get("username", "")})
    result = await db.enquiries.insert_one(doc)
    doc["id"] = str(result.inserted_id); doc.pop("_id", None)
    return doc

@api_router.put("/followups/enquiries/{enq_id}")
async def update_enquiry(enq_id: str, body: EnquiryUpdate, current_user=Depends(verify_token)):
    enq = await db.enquiries.find_one({"_id": obj_id(enq_id)})
    if not enq:
        raise HTTPException(status_code=404, detail="Enquiry not found")
    update = {k: v for k, v in body.dict().items() if v is not None}
    if "mobile" in update:
        update["mobile"] = _mobile10(update["mobile"])
        if len(update["mobile"]) != 10:
            raise HTTPException(status_code=400, detail="Enter a 10-digit mobile number")
    if update.get("status") and update["status"] != enq.get("status"):
        update["closed_at"] = utcnow().isoformat() if update["status"] != "open" else ""
    if update.get("status") == "converted":
        # Bought: make sure they exist as a customer so the sale can be made straight away
        mobile = update.get("mobile") or enq.get("mobile", "")
        cust = await db.customers.find_one({"mobile": mobile})
        if not cust:
            res = await db.customers.insert_one({
                "name": update.get("name") or enq.get("name", ""), "mobile": mobile,
                "email": "", "address": "", "gstin": "", "tags": [], "dob": "",
                "created_at": utcnow().isoformat(),
            })
            update["customer_id"] = str(res.inserted_id)
        else:
            update["customer_id"] = str(cust["_id"])
    await db.enquiries.update_one({"_id": obj_id(enq_id)}, {"$set": update})
    return oid(await db.enquiries.find_one({"_id": obj_id(enq_id)}))

@api_router.delete("/followups/enquiries/{enq_id}")
async def delete_enquiry(enq_id: str, current_user=Depends(require_admin)):
    result = await db.enquiries.delete_one({"_id": obj_id(enq_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Enquiry not found")
    return {"message": "Deleted"}

def insurance_due_date(sale: dict, today):
    """Next renewal on or after 30 days ago. Policies run a year from the insurance
    milestone date (else the sale date); a corrected expiry date, when staff set one, wins.
    The date rolls forward a year at a time, so each sale comes round again every year."""
    expiry = _parse_any_date(sale.get("insurance_expiry"))
    if expiry:
        due = expiry
    else:
        start = _parse_any_date((sale.get("milestone_dates") or {}).get("insurance")) \
                or _parse_any_date(sale.get("sale_date"))
        if not start:
            return None
        due = _add_year(start)
    while due < today - timedelta(days=30):
        due = _add_year(due)
    return due

@api_router.get("/followups/insurance")
async def insurance_due(days: int = Query(30, ge=1, le=120), current_user=Depends(verify_token)):
    today = _today_ist()
    fields = {"customer_name": 1, "customer_mobile": 1, "customer_id": 1, "vehicle_brand": 1,
              "vehicle_model": 1, "vehicle_number": 1, "sale_date": 1, "milestone_dates": 1,
              "insurance_expiry": 1, "insurance_notified_for": 1, "insurance_renewed_for": 1, "status": 1}
    rows = []
    async for s in db.sales.find({"status": {"$ne": "cancelled"}}, fields):
        due = insurance_due_date(s, today)
        if not due or due > today + timedelta(days=days):
            continue
        due_s = due.isoformat()
        if s.get("insurance_renewed_for") == due_s:
            continue
        rows.append({
            "sale_id": str(s["_id"]), "customer_name": s.get("customer_name", ""),
            "customer_mobile": s.get("customer_mobile", ""), "vehicle": f'{s.get("vehicle_brand","")} {s.get("vehicle_model","")}'.strip(),
            "vehicle_number": s.get("vehicle_number", ""), "due": due_s, "days_left": (due - today).days,
            "expiry_set": bool(s.get("insurance_expiry")), "notified": s.get("insurance_notified_for") == due_s,
        })
    rows.sort(key=lambda r: r["due"])
    return rows

class InsuranceAction(BaseModel):
    action: str           = Field(..., pattern=r"^(notified|renewed|set_expiry)$")
    due:    Optional[str] = Field("", pattern=_YMD)   # the renewal this action is about
    expiry: Optional[str] = Field("", pattern=_YMD)   # for set_expiry

@api_router.post("/followups/insurance/{sale_id}")
async def insurance_action(sale_id: str, body: InsuranceAction, current_user=Depends(verify_token)):
    if body.action == "set_expiry":
        if not body.expiry:
            raise HTTPException(status_code=400, detail="Pick the policy expiry date")
        update = {"insurance_expiry": body.expiry}
    else:
        if not body.due:
            raise HTTPException(status_code=400, detail="Renewal date missing")
        update = {f"insurance_{body.action}_for": body.due}
    res = await db.sales.update_one({"_id": obj_id(sale_id)}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Sale not found")
    return {"message": "Saved"}

def next_birthday(dob: str, today):
    d = _parse_any_date(dob)
    if not d:
        return None
    for year in (today.year, today.year + 1):
        try:
            nxt = d.replace(year=year)
        except ValueError:       # 29 Feb in a normal year
            nxt = d.replace(year=year, day=28)
        if nxt >= today:
            return nxt

@api_router.get("/followups/birthdays")
async def upcoming_birthdays(days: int = Query(7, ge=0, le=60), current_user=Depends(verify_token)):
    today = _today_ist()
    rows = []
    async for c in db.customers.find({"dob": {"$nin": ["", None]}}, {"name": 1, "mobile": 1, "dob": 1}):
        nxt = next_birthday(c.get("dob"), today)
        if nxt and (nxt - today).days <= days:
            born = _parse_any_date(c.get("dob"))
            rows.append({"id": str(c["_id"]), "name": c.get("name", ""), "mobile": c.get("mobile", ""),
                         "date": nxt.isoformat(), "days_left": (nxt - today).days, "age": nxt.year - born.year})
    rows.sort(key=lambda r: r["date"])
    return rows

class GreetingSent(BaseModel):
    campaign:    str = Field(..., min_length=1, max_length=80)
    customer_id: str

@api_router.get("/followups/greetings")
async def greeting_recipients(campaign: str = Query(..., min_length=1), current_user=Depends(verify_token)):
    """Every customer with a mobile number, and whether this campaign's message went to them."""
    sent = {d["customer_id"] async for d in db.greetings_sent.find({"campaign": campaign}, {"customer_id": 1})}
    rows = [{"id": str(c["_id"]), "name": c.get("name", ""), "mobile": c.get("mobile", ""),
             "sent": str(c["_id"]) in sent}
            async for c in db.customers.find({"mobile": {"$nin": ["", None]}}, {"name": 1, "mobile": 1}).sort("name", 1)]
    return rows

@api_router.post("/followups/greetings/sent")
async def mark_greeting_sent(body: GreetingSent, current_user=Depends(verify_token)):
    await db.greetings_sent.update_one(
        {"campaign": body.campaign, "customer_id": body.customer_id},
        {"$set": {"sent_at": utcnow().isoformat(), "sent_by": current_user.get("username", "")}},
        upsert=True,
    )
    return {"message": "Saved"}


# ═══════════════════════════════════════════════════════════════════════════════
#  ONLINE SERVICE BOOKING (public page /book)
# ═══════════════════════════════════════════════════════════════════════════════
# A booking becomes a pending job card dated for the chosen day, marked source="online".
BOOKING_DAILY_LIMIT  = 15      # default online bookings per day; the owner changes it on the Service page
BOOKING_CLOSED_DAYS  = {6}     # weekday numbers the shop is shut (6 = Sunday)
BOOKING_DAYS_AHEAD   = 30
BOOKING_IP_LIMIT     = 5       # bookings per hour from one connection, against spam
_booking_hits: dict  = {}      # ip -> recent booking times (in memory; resets on restart)

def booking_open_dates(today):
    """Dates a customer can pick: tomorrow onwards, skipping closed weekdays."""
    days = (today + timedelta(days=i) for i in range(1, BOOKING_DAYS_AHEAD + 1))
    return [d for d in days if d.weekday() not in BOOKING_CLOSED_DAYS]

async def _online_bookings_per_day(dates) -> dict:
    keys = [d.isoformat() for d in dates]
    rows = await db.service_jobs.aggregate([
        {"$match": {"source": "online", "booked_for": {"$in": keys}}},
        {"$group": {"_id": "$booked_for", "n": {"$sum": 1}}},
    ]).to_list(None)
    return {r["_id"]: r["n"] for r in rows}

async def booking_daily_limit() -> int:
    """Online bookings allowed per day (0 = online booking paused). Walk-ins are never refused."""
    doc = await db.settings.find_one({"_id": "booking"})
    return int(doc["daily_limit"]) if doc and doc.get("daily_limit") is not None else BOOKING_DAILY_LIMIT

class BookingSettings(BaseModel):
    daily_limit: int = Field(..., ge=0, le=100)

@api_router.get("/booking/settings")
async def get_booking_settings(current_user=Depends(require_admin)):
    return {"daily_limit": await booking_daily_limit()}

@api_router.put("/booking/settings")
async def set_booking_settings(body: BookingSettings, current_user=Depends(require_admin)):
    await db.settings.update_one({"_id": "booking"}, {"$set": {"daily_limit": body.daily_limit,
                                  "updated_by": current_user.get("username", ""), "updated_at": utcnow().isoformat()}}, upsert=True)
    return {"daily_limit": body.daily_limit}

@api_router.get("/booking/availability")
async def booking_availability():
    """Public. Open dates with the places left, plus the choices the form offers."""
    dates = booking_open_dates(_today_ist())
    limit = await booking_daily_limit()
    taken = await _online_bookings_per_day(dates) if limit else {}
    return {
        "paused":        limit == 0,
        "days":          [{"date": d.isoformat(), "left": max(0, limit - taken.get(d.isoformat(), 0))} for d in dates],
        "service_types": ["General service", *SCHEDULED_SERVICE_TYPES, REPAIR_SERVICE_TYPE],
        "brands":        BRANDS,
    }

class BookingIn(BaseModel):
    name:           str = Field(..., min_length=1, max_length=80)
    mobile:         str = Field(..., min_length=10, max_length=20)
    vehicle_number: str = Field(..., min_length=4, max_length=15)
    brand:          str = Field(..., min_length=2, max_length=30)
    model:          str = Field(..., min_length=1, max_length=60)
    service_type:   str = Field(..., max_length=40)
    complaint:      Optional[str] = Field("", max_length=500)
    date:           str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    website:        Optional[str] = ""   # hidden field: people leave it empty, spam bots fill it in

@api_router.post("/booking", status_code=201)
async def create_booking(body: BookingIn, request: Request):
    """Public. Books a service visit as a pending job card for that day."""
    if body.website:                       # bot: pretend it worked, save nothing
        return {"job_number": "", "date": body.date}
    now = utcnow()
    ip = _client_ip(request)
    recent = [t for t in _booking_hits.get(ip, []) if now - t < timedelta(hours=1)]
    if len(recent) >= BOOKING_IP_LIMIT:
        raise HTTPException(status_code=429, detail="Too many bookings from this connection. Please call the shop.")

    mobile = _mobile10(body.mobile)
    if len(mobile) != 10:
        raise HTTPException(status_code=400, detail="Enter a 10-digit mobile number")
    if body.service_type not in ["General service", *SCHEDULED_SERVICE_TYPES, REPAIR_SERVICE_TYPE]:
        raise HTTPException(status_code=400, detail="Pick a service type from the list")
    day = _parse_any_date(body.date)
    if day not in booking_open_dates(_today_ist()):
        raise HTTPException(status_code=400, detail="That date can't be booked. Pick another day.")
    limit = await booking_daily_limit()
    if limit == 0:
        raise HTTPException(status_code=409, detail="Online booking is paused right now. Please call the shop.")
    if await db.service_jobs.count_documents({"source": "online", "booked_for": body.date, "customer_mobile": mobile}, limit=1):
        raise HTTPException(status_code=409, detail="You already have a booking on this day")
    if (await _online_bookings_per_day([day])).get(body.date, 0) >= limit:
        raise HTTPException(status_code=409, detail="That day just filled up. Please pick another day.")

    cust = await db.customers.find_one({"mobile": mobile})
    if cust:
        cust_id = str(cust["_id"])
    else:
        res = await db.customers.insert_one({"name": body.name.strip(), "mobile": mobile, "email": "", "address": "",
                                             "gstin": "", "tags": [], "dob": "", "created_at": now.isoformat()})
        cust_id = str(res.inserted_id)

    job = await create_service_job(ServiceJobCreate(
        customer_id=cust_id, vehicle_number=body.vehicle_number, brand=body.brand, model=body.model.strip(),
        complaint=(body.complaint or "").strip() or body.service_type,
        check_in_date=day.strftime("%d %b %Y"),
        service_type="" if body.service_type == "General service" else body.service_type,
        notes=f"Online booking for {day.strftime('%a %d %b')}",
    ), current_user={})
    await db.service_jobs.update_one({"_id": obj_id(job["id"])}, {"$set": {"source": "online", "booked_for": body.date}})
    _booking_hits[ip] = recent + [now]
    return {"job_number": job["job_number"], "date": body.date}
