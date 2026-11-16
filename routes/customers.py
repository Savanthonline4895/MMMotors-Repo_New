"""API routes: customers (split out of server.py; code moved verbatim)."""
import asyncio
import re
from typing import Optional
from fastapi import Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from database import verify_token, require_admin, oid, oids, obj_id, utcnow
from models import BadgeTypeCreate, BadgeTypeUpdate, CustomerCreate, CustomerTagsUpdate, CustomerUpdate
from routers import api_router

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

# ═══════════════════════════════════════════════════════════════════════════════
#  CUSTOMERS
# ═══════════════════════════════════════════════════════════════════════════════

@api_router.get("/customers")
async def list_customers(
    search: Optional[str] = Query(None),
    tag:    Optional[str] = Query(None),
    limit:  int           = Query(200, ge=1, le=2000),
    current_user=Depends(verify_token),
):
    query: dict = {}
    if search:
        s = re.escape(search)
        query["$or"] = [
            {"name":   {"$regex": s, "$options": "i"}},
            {"mobile": {"$regex": s, "$options": "i"}},
            {"email":  {"$regex": s, "$options": "i"}},
        ]
    if tag:
        query["tags"] = tag
    docs  = await db.customers.find(query).sort("name", 1).limit(limit).to_list(limit)
    total = await db.customers.count_documents(query)
    return JSONResponse(content=oids(docs), headers={"X-Total-Count": str(total)})

@api_router.post("/customers", status_code=201)
async def create_customer(body: CustomerCreate, current_user=Depends(verify_token)):
    doc = body.dict()
    doc["created_at"] = utcnow().isoformat()
    result = await db.customers.insert_one(doc)
    doc["id"] = str(result.inserted_id); doc.pop("_id", None)
    return doc

@api_router.get("/customers/{cust_id}")
async def get_customer(cust_id: str, current_user=Depends(verify_token)):
    doc = await db.customers.find_one({"_id": obj_id(cust_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Customer not found")
    return oid(doc)

@api_router.put("/customers/{cust_id}")
async def update_customer(cust_id: str, body: CustomerUpdate, current_user=Depends(verify_token)):
    update = {k: v for k, v in body.dict().items() if v is not None}
    await db.customers.update_one({"_id": obj_id(cust_id)}, {"$set": update})
    return oid(await db.customers.find_one({"_id": obj_id(cust_id)}))

@api_router.delete("/customers/{cust_id}")
async def delete_customer(cust_id: str, current_user=Depends(require_admin)):
    result = await db.customers.delete_one({"_id": obj_id(cust_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Customer not found")
    return {"message": "Deleted"}


@api_router.put("/customers/{cust_id}/tags")
async def set_customer_tags(cust_id: str, body: CustomerTagsUpdate, current_user=Depends(require_admin)):
    """Owner-only. Replace a customer's badge list. Silently drops any name not in badge_types."""
    valid = set()
    async for b in db.badge_types.find({}, {"name": 1}):
        valid.add(b["name"])
    clean = [t for t in (body.tags or []) if t in valid]
    cust = await db.customers.find_one({"_id": obj_id(cust_id)}, {"_id": 1})
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")
    await db.customers.update_one({"_id": obj_id(cust_id)}, {"$set": {"tags": clean}})
    return {"id": cust_id, "tags": clean}


@api_router.get("/customer-badges-map")
async def customer_badges_map(current_user=Depends(verify_token)):
    """Lightweight lookup: mobile → [badge names]. Used by list pages that show a customer name
    but don't already fetch the full customer record. Empty tag lists are omitted."""
    out = {}
    cursor = db.customers.find({"tags": {"$exists": True, "$ne": []}}, {"mobile": 1, "tags": 1})
    async for c in cursor:
        mob = (c.get("mobile") or "").strip()
        tags = c.get("tags") or []
        if mob and tags:
            out[mob] = tags
    return out


# ═══════════════════════════════════════════════════════════════════════════════
#  BADGE TYPES  (owner-managed set of labels applied to customers)
# ═══════════════════════════════════════════════════════════════════════════════

@api_router.get("/badge-types")
async def list_badge_types(current_user=Depends(verify_token)):
    """All roles can read — needed to render chips throughout the app."""
    docs = await db.badge_types.find({}).sort([("sort_order", 1), ("name", 1)]).to_list(None)
    return oids(docs)

@api_router.post("/badge-types")
async def create_badge_type(body: BadgeTypeCreate, current_user=Depends(require_admin)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name required")
    if await db.badge_types.find_one({"name": name}):
        raise HTTPException(status_code=409, detail=f"Badge '{name}' already exists")
    doc = {
        "name":       name,
        "color":      body.color,
        "sort_order": body.sort_order if body.sort_order is not None else 100,
        "created_at": utcnow().isoformat(),
    }
    res = await db.badge_types.insert_one(doc)
    doc["id"] = str(res.inserted_id); doc.pop("_id", None)
    return doc

@api_router.patch("/badge-types/{badge_id}")
async def update_badge_type(badge_id: str, body: BadgeTypeUpdate, current_user=Depends(require_admin)):
    existing = await db.badge_types.find_one({"_id": obj_id(badge_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Badge type not found")
    update = {k: v for k, v in body.dict().items() if v is not None}
    if "name" in update:
        update["name"] = update["name"].strip()
        if not update["name"]:
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        clash = await db.badge_types.find_one({"name": update["name"], "_id": {"$ne": obj_id(badge_id)}})
        if clash:
            raise HTTPException(status_code=409, detail=f"Badge '{update['name']}' already exists")
        old_name = existing["name"]
        new_name = update["name"]
        if old_name != new_name:
            # Cascade rename across all customers
            await db.customers.update_many(
                {"tags": old_name},
                {"$set": {"tags.$[elem]": new_name}},
                array_filters=[{"elem": old_name}],
            )
    await db.badge_types.update_one({"_id": obj_id(badge_id)}, {"$set": update})
    return oid(await db.badge_types.find_one({"_id": obj_id(badge_id)}))

@api_router.delete("/badge-types/{badge_id}")
async def delete_badge_type(badge_id: str, current_user=Depends(require_admin)):
    doc = await db.badge_types.find_one({"_id": obj_id(badge_id)}, {"name": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="Badge type not found")
    strip_res = await db.customers.update_many({"tags": doc["name"]}, {"$pull": {"tags": doc["name"]}})
    await db.badge_types.delete_one({"_id": obj_id(badge_id)})
    return {"message": "Deleted", "customers_updated": strip_res.modified_count}



@api_router.get("/customers/{cust_id}/timeline")
async def customer_timeline(cust_id: str, current_user=Depends(verify_token)):
    sales, jobs = await asyncio.gather(
        db.sales.find({"customer_id": cust_id}).sort("sale_date", -1).to_list(None),
        db.service_jobs.find({"customer_id": cust_id}).sort("check_in_date", -1).to_list(None),
    )
    job_ids = [str(j["_id"]) for j in jobs]
    bills = []
    if job_ids:
        bills = await db.service_bills.find({"job_id": {"$in": job_ids}}).to_list(None)
    service_spend       = sum(b.get("grand_total", 0) for b in bills)
    total_sales_spend   = sum(s.get("total_amount", 0) for s in sales)
    return {
        "sales":             oids(sales),
        "service":           oids(jobs),
        "total_spent":       total_sales_spend + service_spend,
        "service_spend":     service_spend,
        "total_sales_spend": total_sales_spend,
    }
