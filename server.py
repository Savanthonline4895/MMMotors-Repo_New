"""
MM Motors — Backend (FastAPI + MongoDB Atlas)

Structure:
  database.py — Shared config, DB helpers, auth, GST utils, serialization
  server.py   — App setup, Pydantic models, all route handlers

Render env vars required:
  MONGO_URL       — MongoDB Atlas connection string
  ANTHROPIC_API_KEY — Claude API key; only needed to read supplier bills from a PDF or photo
  DB_NAME         — Database name (default: mmmotors)
  JWT_SECRET_KEY  — Random 64-char hex (generate: python3 -c "import secrets; print(secrets.token_hex(32))")
  ALLOW_ORIGINS   — Your Vercel URL (e.g. https://yourapp.vercel.app)
"""

import secrets
import re
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pymongo.errors import OperationFailure
import os
import certifi
import database as _db
from database import MONGO_URL, DB_NAME, LOGIN_LOCKOUT_MIN, pwd_ctx, utcnow, ist_now, AUDIT_SKIP, audit_diff, audit_ref, audit_jsonable, VEHICLE_STATUSES, canon_vehicle_status, canon_vehicle_type
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket
from common import _AUDIT_COLLECTIONS
from routers import api_router, import_router

# Module-level db/fs aliases — set in lifespan
db = None
fs = None

async def _normalize_vehicle_fields():
    """Idempotent: rewrite known status/type spellings ('Instock', 'In Stock', 'NEW')
    to canonical values so count_documents({"status": "in_stock"}) is exact everywhere."""
    for field, canon in (("status", canon_vehicle_status), ("type", canon_vehicle_type)):
        for raw in await _db.db.vehicles.distinct(field):
            target = canon(raw)
            if raw != target and (field == "type" or target in VEHICLE_STATUSES):
                res = await _db.db.vehicles.update_many({field: raw}, {"$set": {field: target}})
                print(f"[MM Motors] vehicles.{field}: {raw!r} → {target!r} ({res.modified_count})")
        # missing field entirely
        default = "in_stock" if field == "status" else "new"
        res = await _db.db.vehicles.update_many({field: {"$exists": False}}, {"$set": {field: default}})
        if res.modified_count:
            print(f"[MM Motors] vehicles.{field}: missing → {default!r} ({res.modified_count})")

@asynccontextmanager
async def lifespan(app):
    global db, fs
    try:
        _db.client = AsyncIOMotorClient(
            MONGO_URL,
            serverSelectionTimeoutMS=10000,
            tls=True,
            tlsCAFile=certifi.where(),
        )
        _db.db = _db.client[DB_NAME]
        _db.fs = AsyncIOMotorGridFSBucket(_db.db)
        db = _db.db
        fs = _db.fs
        for m in _DB_MODULES:          # route modules keep their own db/fs globals
            m.db, m.fs = db, fs
        await _ensure_indexes()
        await _seed_owner()
        await _seed_badge_types()
        await _normalize_vehicle_fields()
        print(f"[MM Motors] Connected to MongoDB · DB: {DB_NAME}")
    except Exception as e:
        print(f"[MM Motors] WARNING: DB connection failed: {e}")

    yield

    if _db.client:
        _db.client.close()

# ─── App ──────────────────────────────────────────────────────────────────────
_raw_origins = os.getenv("ALLOW_ORIGINS", "").strip()
ALLOW_ORIGINS = [o.strip().rstrip("/") for o in _raw_origins.split(",") if o.strip()]
if not ALLOW_ORIGINS or "*" in ALLOW_ORIGINS:
    # Never fall back to "*": with allow_credentials=True that lets any site make
    # cookie-authenticated requests. Without a list, only the local dev server is allowed.
    print("[MM Motors] WARNING: ALLOW_ORIGINS is unset or contains '*'. "
          "Set it to the frontend URL; allowing only http://localhost:5173 for now.")
    ALLOW_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]

app = FastAPI(title="MM Motors API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count"],
)

# CSRF guard — the mm_token cookie is SameSite=None, so any site could make the browser
# send it. State-changing requests that carry the cookie must come from an allowed origin.
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

@app.middleware("http")
async def _csrf_origin_guard(request: Request, call_next):
    if request.method in _UNSAFE_METHODS and request.cookies.get("mm_token"):
        origin = request.headers.get("origin")
        if not origin:
            referer = request.headers.get("referer", "")
            parts = referer.split("/")
            origin = "/".join(parts[:3]) if len(parts) >= 3 else ""
        if origin.rstrip("/") not in ALLOW_ORIGINS:
            return JSONResponse(status_code=403, content={"detail": "Request origin not allowed"})
    return await call_next(request)

# Cache invalidation — clears /service/due cache on mutations to sales/service_jobs
@app.middleware("http")
async def _service_due_cache_invalidator(request: Request, call_next):
    response = await call_next(request)
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and 200 <= response.status_code < 300:
        path = request.url.path
        if "/sales" in path or "/service" in path:
            _invalidate_service_due_cache()
    return response
_AUDIT_PATH = re.compile(r"^/api/(" + "|".join(_AUDIT_COLLECTIONS) + r")/([0-9a-f]{24})(?:/([\w-]+))?$")
@app.middleware("http")
async def _audit_trail(request: Request, call_next):
    m = _AUDIT_PATH.match(request.url.path) if request.method in ("PUT", "PATCH", "DELETE", "POST") else None
    if not m or (request.method == "POST" and not m.group(3)) or _db.db is None:
        return await call_next(request)
    from bson import ObjectId as _OID
    coll = _db.db[_AUDIT_COLLECTIONS[m.group(1)]]
    try:
        before = await coll.find_one({"_id": _OID(m.group(2))})
    except Exception:
        before = None
    response = await call_next(request)
    if before is None or not 200 <= response.status_code < 300:
        return response
    try:
        after = await coll.find_one({"_id": before["_id"]})
        entry = {
            "at":        utcnow(),
            "user":      getattr(request.state, "audit_user", "") or "",
            "action":    "delete" if after is None else (m.group(3) or "edit"),
            "entity":    m.group(1),
            "entity_id": m.group(2),
            "ref":       audit_ref(before),
        }
        if after is None:
            entry["deleted"] = audit_jsonable({k: v for k, v in before.items()
                                                if k not in AUDIT_SKIP and k != "password"})
        else:
            entry["changes"] = audit_diff(before, after)
            if not entry["changes"]:
                return response
        await _db.db.audit_log.insert_one(entry)
    except Exception as e:   # history is a record, never a reason to fail the user's save
        print(f"WARNING: change-history write failed for {request.url.path}: {e}")
    return response

async def _ensure_indexes():
    # users
    await db.users.create_index("username", unique=True)
    await db.users.create_index("mobile")
    # customers
    await db.customers.create_index("mobile")
    await db.customers.create_index([("name","text"),("mobile","text")])
    # vehicles
    await db.vehicles.create_index("chassis_number", unique=True, sparse=True)
    await db.vehicles.create_index("vehicle_number")
    await db.vehicles.create_index("status")
    await db.vehicles.create_index("brand")
    # sales
    await db.sales.create_index("invoice_number", unique=True)
    await db.sales.create_index("customer_id")
    await db.sales.create_index("sale_date")
    await db.sales.create_index("chassis_number")
    await db.sales.create_index("vehicle_number")
    await db.sales.create_index("customer_mobile")
    # service_jobs
    await db.service_jobs.create_index("job_number", unique=True)
    await db.service_jobs.create_index("status")
    await db.service_jobs.create_index("customer_id")
    await db.service_jobs.create_index("vehicle_number")
    await db.service_jobs.create_index("feedback_token", unique=True, sparse=True)
    await db.service_jobs.create_index([("source", 1), ("booked_for", 1)])
    # follow-ups
    await db.enquiries.create_index([("status", 1), ("follow_up_date", 1)])
    await db.enquiries.create_index("mobile")
    await db.greetings_sent.create_index([("campaign", 1), ("customer_id", 1)], unique=True)
    # service_bills
    await db.service_bills.create_index("bill_number", unique=True)
    await db.service_bills.create_index("job_id")
    # spare_parts
    await db.spare_parts.create_index("part_number", unique=True)
    await db.spare_parts.create_index("category")
    await db.spare_parts.create_index([("name","text"),("part_number","text")])
    # parts_bills  ← PATCH 7: new collection index
    # bill_number used to be a plain index; unique stops a reset counter from silently repeating numbers.
    if not (await db.parts_bills.index_information()).get("bill_number_1", {}).get("unique"):
        try:
            await db.parts_bills.drop_index("bill_number_1")
        except OperationFailure:
            pass  # fresh database, no index yet
        try:
            await db.parts_bills.create_index("bill_number", unique=True)
        except OperationFailure as e:
            # Duplicates already stored: keep the plain index so startup continues; owner fixes them by hand.
            await db.parts_bills.create_index("bill_number")
            print(f"[MM Motors] WARNING: duplicate parts_bills.bill_number values, unique index not created: {e}")
    await db.parts_bills.create_index("customer_mobile")
    await db.parts_bills.create_index("bill_date")
    # vendors — spare-parts suppliers
    await db.vendors.create_index("name")
    await db.vendors.create_index("gstin", sparse=True)
    # purchase_bills — one doc per vendor invoice; multi-line
    await db.purchase_bills.create_index([("vendor_id", 1), ("bill_number", 1)], unique=True, sparse=True)
    await db.purchase_bills.create_index("bill_date")
    await db.purchase_bills.create_index("vendor_id")
    # spare_parts additions: index the alias list for vendor-scoped search
    await db.spare_parts.create_index("aliases.vendor_id")
    await db.spare_parts.create_index("aliases.part_number")
    # login_attempts — TTL 30 min
    await db.login_attempts.create_index(
        "created_at", expireAfterSeconds=LOGIN_LOCKOUT_MIN * 60
    )
    await db.login_attempts.create_index("username")
    await db.vehicle_bookings.create_index("booking_number", unique=True)
    await db.vehicle_bookings.create_index("status")
    # change history — kept 2 years, then Mongo drops it (M0 has a 512 MB cap)
    await db.audit_log.create_index("at", expireAfterSeconds=2 * 365 * 24 * 3600)
    await db.audit_log.create_index([("entity_id", 1), ("at", -1)])
    await db.login_attempts.create_index([("username", 1), ("ip", 1)])
    await db.badge_types.create_index("name", unique=True)
    print("[MM Motors] Indexes ensured")

async def _seed_owner():
    """Create the first owner account on an empty install only. Existing accounts are never
    touched, so deactivating or demoting 'owner' sticks across restarts."""
    if await db.users.find_one({"role": "owner"}) or await db.users.find_one({"username": "owner"}):
        return
    initial = os.getenv("OWNER_INITIAL_PASSWORD", "").strip()
    generated = not initial
    if generated:
        initial = secrets.token_urlsafe(12)
    await db.users.insert_one({
        "username":   "owner",
        "name":       "Owner",
        "mobile":     "",
        "email":      "",
        "role":       "owner",
        "password":   pwd_ctx.hash(initial),
        "status":     "active",
        "salary":     0,
        "join_date":  ist_now().strftime("%d %b %Y"),
        "created_at": utcnow().isoformat(),
    })
    if generated:
        # Shown once, on the first boot of an empty database. Change it after signing in.
        print(f"[MM Motors] Owner account created  username=owner  one-time password={initial}")
    else:
        print("[MM Motors] Owner account created from OWNER_INITIAL_PASSWORD")

async def _seed_badge_types():
    """Seed default badge types once. Owner can add/edit more from Settings later."""
    if await db.badge_types.count_documents({}) > 0:
        return
    defaults = [
        {"name": "VIP",           "color": "#B8860B", "sort_order":  1},
        {"name": "Family",        "color": "#9333EA", "sort_order":  2},
        {"name": "Repeat",        "color": "#16A34A", "sort_order":  3},
        {"name": "Referral",      "color": "#0EA5E9", "sort_order":  4},
        {"name": "Special Offer", "color": "#EF4444", "sort_order":  5},
        {"name": "Corporate",     "color": "#3B82F6", "sort_order":  6},
    ]
    now = utcnow().isoformat()
    for b in defaults:
        b["created_at"] = now
    await db.badge_types.insert_many(defaults)
    print(f"[MM Motors] Seeded {len(defaults)} default badge types")


# ═══════════════════════════════════════════════════════════════════════════════
#  HEALTH
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {"status": "ok", "service": "MM Motors API", "version": "2.0.0"}

@app.get("/ready")
async def ready():
    try:
        await db.command("ping")
        return {"status": "ready", "db": DB_NAME}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "not_ready", "error": str(e)})


# Route modules register their endpoints on api_router / import_router when imported,
# in the order the sections used to appear in this file (route order matters).
import common
import routes.files
import routes.auth
import routes.customers
import routes.vehicles
import routes.sales
import routes.service
import routes.service_bills
import routes.purchasing
import routes.parts
import routes.reports
import routes.imports
import routes.ledger
import routes.backup
import routes.accident
import routes.followups
import routes.vehicle_bookings
_DB_MODULES = [common, routes.files, routes.auth, routes.customers, routes.vehicles, routes.sales, routes.service, routes.service_bills, routes.purchasing, routes.parts, routes.reports, routes.imports, routes.ledger, routes.backup, routes.accident, routes.followups, routes.vehicle_bookings]
# Helpers server.py itself uses (middleware, lifespan) — imported after registration
from routes.service import _invalidate_service_due_cache

app.include_router(api_router)
app.include_router(import_router)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
