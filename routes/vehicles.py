"""API routes: vehicles (split out of server.py; code moved verbatim)."""
import re
from typing import Optional
from fastapi import Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from database import verify_token, require_admin, oid, oids, obj_id, utcnow, VEHICLE_STATUSES, canon_vehicle_status, canon_vehicle_type
from models import VehicleCreate, VehicleUpdate
from routers import api_router

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

# ═══════════════════════════════════════════════════════════════════════════════
#  VEHICLES
# ═══════════════════════════════════════════════════════════════════════════════

@api_router.get("/vehicles")
async def list_vehicles(
    brand:   Optional[str] = Query(None),
    status:  Optional[str] = Query(None),
    type:    Optional[str] = Query(None),
    search:  Optional[str] = Query(None),
    limit:   int           = Query(300, ge=1, le=5000),
    current_user=Depends(verify_token),
):
    query: dict = {}
    if brand:  query["brand"]  = brand.upper()
    if status: query["status"] = canon_vehicle_status(status)
    if type:   query["type"]   = canon_vehicle_type(type)
    if search:
        s = re.escape(search)
        query["$or"] = [
            {"model":          {"$regex": s, "$options": "i"}},
            {"chassis_number": {"$regex": s, "$options": "i"}},
            {"vehicle_number": {"$regex": s, "$options": "i"}},
            {"color":          {"$regex": s, "$options": "i"}},
        ]
    docs  = await db.vehicles.find(query).sort("created_at", -1).limit(limit).to_list(limit)
    total = await db.vehicles.count_documents(query)
    return JSONResponse(content=oids(docs), headers={"X-Total-Count": str(total)})

@api_router.post("/vehicles", status_code=201)
async def create_vehicle(body: VehicleCreate, current_user=Depends(verify_token)):
    chassis = body.chassis_number.strip().upper()
    if await db.vehicles.find_one({"chassis_number": chassis}):
        raise HTTPException(status_code=409, detail=f"Chassis number {chassis} already exists")
    doc = body.dict()
    doc["chassis_number"] = chassis
    doc["brand"]          = doc["brand"].upper()
    doc["status"]         = canon_vehicle_status(doc.get("status"))
    doc["type"]           = canon_vehicle_type(doc.get("type"))
    doc["created_at"]     = utcnow().isoformat()
    result = await db.vehicles.insert_one(doc)
    doc["id"] = str(result.inserted_id); doc.pop("_id", None)
    return doc

@api_router.get("/vehicles/stats/summary")
async def vehicle_stats(
    brand: Optional[str] = Query(None),
    current_user=Depends(verify_token),
):
    """Counts by status + type, optionally for one brand, plus per-brand breakdown.
    Statuses are canonicalised on write and at startup; anything still unrecognised is
    reported under `other` / `unknown_statuses` instead of silently vanishing."""
    match = {"brand": brand.upper()} if brand else {}
    rows = await db.vehicles.aggregate([
        {"$match": match},
        {"$group": {
            "_id": {"brand": "$brand", "status": "$status", "type": "$type"},
            "n": {"$sum": 1},
            "value": {"$sum": {"$ifNull": ["$purchase_price", 0]}},
        }},
    ]).to_list(None)

    out = {"total": 0, "in_stock": 0, "sold": 0, "in_service": 0, "returned": 0,
           "other": 0, "new": 0, "used": 0, "stock_value": 0}
    by_brand: dict = {}
    unknown: dict = {}
    for r in rows:
        b  = (r["_id"].get("brand") or "UNKNOWN")
        st = canon_vehicle_status(r["_id"].get("status"))
        ty = canon_vehicle_type(r["_id"].get("type"))
        n  = r["n"]
        bucket = st if st in VEHICLE_STATUSES else "other"
        if bucket == "other":
            raw = str(r["_id"].get("status"))
            unknown[raw] = unknown.get(raw, 0) + n
        out["total"] += n
        out[bucket]  += n
        out[ty]      += n
        if st == "in_stock":
            out["stock_value"] += r["value"] or 0
        bb = by_brand.setdefault(b, {"brand": b, "total": 0, "in_stock": 0, "sold": 0,
                                     "in_service": 0, "returned": 0, "other": 0})
        bb["total"] += n
        bb[bucket]  += n

    out["by_brand"] = sorted(by_brand.values(), key=lambda x: (-x["in_stock"], x["brand"]))
    out["unknown_statuses"] = unknown
    return out

@api_router.get("/vehicles/{veh_id}")
async def get_vehicle(veh_id: str, current_user=Depends(verify_token)):
    doc = await db.vehicles.find_one({"_id": obj_id(veh_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return oid(doc)

@api_router.put("/vehicles/{veh_id}")
async def update_vehicle(veh_id: str, body: VehicleUpdate, current_user=Depends(verify_token)):
    update = {k: v for k, v in body.dict().items() if v is not None}
    if "chassis_number" in update:
        update["chassis_number"] = update["chassis_number"].strip().upper()
    if "brand" in update:
        update["brand"] = update["brand"].upper()
    if "status" in update:
        update["status"] = canon_vehicle_status(update["status"])
    if "type" in update:
        update["type"] = canon_vehicle_type(update["type"])
    await db.vehicles.update_one({"_id": obj_id(veh_id)}, {"$set": update})
    return oid(await db.vehicles.find_one({"_id": obj_id(veh_id)}))

@api_router.delete("/vehicles/{veh_id}")
async def delete_vehicle(veh_id: str, current_user=Depends(require_admin)):
    result = await db.vehicles.delete_one({"_id": obj_id(veh_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return {"message": "Deleted"}
