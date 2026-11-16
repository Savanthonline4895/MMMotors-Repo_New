"""API routes: accident (split out of server.py; code moved verbatim)."""
from fastapi import Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from database import verify_token, require_admin, next_sequence, oid, oids, obj_id, utcnow
from models import AccidentEstimateCreate, AccidentEstimateUpdate
from routers import api_router

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

# ═══════════════════════════════════════════════════════════════════════════════
#  Accident Estimates
# ═══════════════════════════════════════════════════════════════════════════════

@api_router.get("/accident-estimates")
async def list_accident_estimates(
    skip:  int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user=Depends(verify_token),
):
    cursor = db.accident_estimates.find().sort("created_at", -1).skip(skip).limit(limit)
    docs   = await cursor.to_list(limit)
    total  = await db.accident_estimates.count_documents({})
    return JSONResponse(content=oids(docs), headers={"X-Total-Count": str(total)})


@api_router.post("/accident-estimates", status_code=201)
async def create_accident_estimate(body: AccidentEstimateCreate, current_user=Depends(verify_token)):
    est_number = await next_sequence("accident_estimate")   # already "EST-0001"
    ts         = utcnow().isoformat()
    doc = {
        **body.model_dump(),
        "estimate_number": est_number,
        "created_by":      current_user["username"],
        "created_at":      ts,
        "updated_at":      ts,
    }
    result  = await db.accident_estimates.insert_one(doc)
    created = await db.accident_estimates.find_one({"_id": result.inserted_id})
    return oid(created)


@api_router.get("/accident-estimates/{est_id}")
async def get_accident_estimate(est_id: str, current_user=Depends(verify_token)):
    doc = await db.accident_estimates.find_one({"_id": obj_id(est_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Estimate not found")
    return oid(doc)


@api_router.put("/accident-estimates/{est_id}")
async def update_accident_estimate(est_id: str, body: AccidentEstimateUpdate, current_user=Depends(verify_token)):
    existing = await db.accident_estimates.find_one({"_id": obj_id(est_id)})
    if not existing:
        # Estimate was deleted — create fresh so frontend gets a new id back
        est_number = await next_sequence("accident_estimate")
        ts  = utcnow().isoformat()
        doc = {
            **body.model_dump(exclude_none=True),
            "estimate_number": est_number,
            "created_by":      current_user["username"],
            "created_at":      ts,
            "updated_at":      ts,
        }
        result  = await db.accident_estimates.insert_one(doc)
        created = await db.accident_estimates.find_one({"_id": result.inserted_id})
        return oid(created)
    updates = body.model_dump(exclude_none=True)
    updates["updated_at"] = utcnow().isoformat()
    await db.accident_estimates.update_one({"_id": obj_id(est_id)}, {"$set": updates})
    return oid(await db.accident_estimates.find_one({"_id": obj_id(est_id)}))


@api_router.delete("/accident-estimates/{est_id}", status_code=204)
async def delete_accident_estimate(est_id: str, current_user=Depends(require_admin)):
    result = await db.accident_estimates.delete_one({"_id": obj_id(est_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Estimate not found")


@api_router.post("/migrations/rebuild-customers")
async def rebuild_customers(current_user=Depends(require_admin)):
    """Rebuild customers collection from denormalized data in sales + service_jobs.
    Deduplicates by mobile number. Skips mobiles that already exist in customers."""
    seen = set()
    created = 0
    skipped = 0

    # Gather existing customer mobiles to skip
    existing = await db.customers.find({}, {"mobile": 1}).to_list(100000)
    for c in existing:
        if c.get("mobile"):
            seen.add(c["mobile"].strip())

    # Scan sales
    async for sale in db.sales.find(
        {"customer_mobile": {"$exists": True, "$ne": ""}},
        {"customer_name": 1, "customer_mobile": 1, "customer_address": 1, "care_of": 1, "created_at": 1}
    ):
        mobile = (sale.get("customer_mobile") or "").strip()
        if not mobile or mobile in seen:
            skipped += 1
            continue
        seen.add(mobile)
        await db.customers.insert_one({
            "name":       sale.get("customer_name", ""),
            "mobile":     mobile,
            "email":      "",
            "address":    sale.get("customer_address", ""),
            "care_of":    sale.get("care_of", ""),
            "tags":       [],
            "gstin":      "",
            "created_at": sale.get("created_at", utcnow().isoformat()),
            "_rebuilt":   True,
        })
        created += 1

    # Scan service_jobs
    async for job in db.service_jobs.find(
        {"customer_mobile": {"$exists": True, "$ne": ""}},
        {"customer_name": 1, "customer_mobile": 1, "customer_address": 1, "created_at": 1}
    ):
        mobile = (job.get("customer_mobile") or "").strip()
        if not mobile or mobile in seen:
            skipped += 1
            continue
        seen.add(mobile)
        await db.customers.insert_one({
            "name":       job.get("customer_name", ""),
            "mobile":     mobile,
            "email":      "",
            "address":    job.get("customer_address", ""),
            "tags":       [],
            "gstin":      "",
            "created_at": job.get("created_at", utcnow().isoformat()),
            "_rebuilt":   True,
        })
        created += 1

    return {"created": created, "skipped": skipped,
            "message": f"✅ {created} customers rebuilt from sales + service records."}


@api_router.post("/migrations/relink-customers")
async def relink_customers(current_user=Depends(require_admin)):
    """After rebuild: update customer_id on sales + service_jobs to match rebuilt customers by mobile."""
    relinked_sales = 0
    relinked_jobs = 0

    async for sale in db.sales.find(
        {"customer_mobile": {"$exists": True, "$ne": ""}},
        {"_id": 1, "customer_mobile": 1, "customer_id": 1}
    ):
        mobile = (sale.get("customer_mobile") or "").strip()
        if not mobile:
            continue
        cust = await db.customers.find_one({"mobile": mobile}, {"_id": 1})
        if cust and str(cust["_id"]) != sale.get("customer_id", ""):
            await db.sales.update_one(
                {"_id": sale["_id"]},
                {"$set": {"customer_id": str(cust["_id"])}}
            )
            relinked_sales += 1

    async for job in db.service_jobs.find(
        {"customer_mobile": {"$exists": True, "$ne": ""}},
        {"_id": 1, "customer_mobile": 1, "customer_id": 1}
    ):
        mobile = (job.get("customer_mobile") or "").strip()
        if not mobile:
            continue
        cust = await db.customers.find_one({"mobile": mobile}, {"_id": 1})
        if cust and str(cust["_id"]) != job.get("customer_id", ""):
            await db.service_jobs.update_one(
                {"_id": job["_id"]},
                {"$set": {"customer_id": str(cust["_id"])}}
            )
            relinked_jobs += 1

    return {"relinked_sales": relinked_sales, "relinked_jobs": relinked_jobs,
            "message": f"✅ Re-linked {relinked_sales} sales + {relinked_jobs} service jobs to rebuilt customers."}
