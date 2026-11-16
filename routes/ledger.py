"""API routes: ledger (split out of server.py; code moved verbatim)."""
import asyncio
import re
from typing import Optional
from fastapi import Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from database import verify_token, require_admin, oid, oids, obj_id, utcnow, ist_now
from routes.imports import EXPENSE_CATEGORIES
from routers import api_router

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

# ═══════════════════════════════════════════════════════════════════════════════
#  Debt Ledger
# ═══════════════════════════════════════════════════════════════════════════════

class DebtCreate(BaseModel):
    customer_id:   str
    amount:        float = Field(..., ge=0)
    description:   Optional[str] = ""
    due_date:      Optional[str] = ""
    source:        Optional[str] = "manual"   # manual | sale | service

class PaymentCreate(BaseModel):
    amount:     float = Field(..., gt=0)
    notes:      Optional[str] = ""
    paid_date:  Optional[str] = ""

@api_router.get("/debts")
async def list_debts(
    customer_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 200,
    current_user=Depends(verify_token)
):
    q: dict = {}
    if customer_id: q["customer_id"] = customer_id
    if status:      q["status"] = status
    cursor = db.debts.find(q).sort("created_at", -1).limit(limit)
    return oids(await cursor.to_list(length=limit))

@api_router.post("/debts", status_code=201)
async def create_debt(body: DebtCreate, current_user=Depends(verify_token)):
    customer = await db.customers.find_one({"_id": obj_id(body.customer_id)})
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    doc = {
        "customer_id":    body.customer_id,
        "customer_name":  customer["name"],
        "customer_mobile": customer.get("mobile", ""),
        "amount":         body.amount,
        "paid":           0.0,
        "balance":        body.amount,
        "description":    body.description or "",
        "due_date":       body.due_date or "",
        "source":         body.source or "manual",
        "status":         "pending",
        "payments":       [],
        "created_at":     utcnow().isoformat(),
    }
    result = await db.debts.insert_one(doc)
    doc["id"] = str(result.inserted_id); doc.pop("_id", None)
    return doc

@api_router.get("/debts/summary")
async def debt_summary(current_user=Depends(verify_token)):
    pipeline = [
        {"$group": {
            "_id": "$status",
            "total_amount":  {"$sum": "$amount"},
            "total_balance": {"$sum": "$balance"},
            "count":         {"$sum": 1},
        }}
    ]
    rows = await db.debts.aggregate(pipeline).to_list(length=20)
    return rows

@api_router.post("/debts/{debt_id}/payments", status_code=201)
async def add_payment(debt_id: str, body: PaymentCreate, current_user=Depends(verify_token)):
    debt = await db.debts.find_one({"_id": obj_id(debt_id)})
    if not debt:
        raise HTTPException(status_code=404, detail="Debt not found")
    payment = {
        "amount":    body.amount,
        "notes":     body.notes or "",
        "paid_date": body.paid_date or ist_now().strftime("%Y-%m-%d"),
        "recorded_at": utcnow().isoformat(),
        "recorded_by": current_user.get("name", ""),
    }
    new_paid    = round(debt.get("paid", 0) + body.amount, 2)
    new_balance = round(debt["amount"] - new_paid, 2)
    new_status  = "paid" if new_balance <= 0 else ("partial" if new_paid > 0 else "pending")
    await db.debts.update_one(
        {"_id": obj_id(debt_id)},
        {"$push": {"payments": payment},
         "$set":  {"paid": new_paid, "balance": max(new_balance, 0), "status": new_status}}
    )
    updated = await db.debts.find_one({"_id": obj_id(debt_id)})
    return oid(updated)

@api_router.put("/debts/{debt_id}")
async def update_debt(debt_id: str, body: dict, current_user=Depends(verify_token)):
    allowed = {"description", "due_date", "amount", "status"}
    update  = {k: v for k, v in body.items() if k in allowed}
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")
    # recalc balance if amount changed
    debt = await db.debts.find_one({"_id": obj_id(debt_id)})
    if "amount" in update:
        update["balance"] = max(round(update["amount"] - debt.get("paid", 0), 2), 0)
        update["status"]  = "paid" if update["balance"] <= 0 else ("partial" if debt.get("paid",0)>0 else "pending")
    await db.debts.update_one({"_id": obj_id(debt_id)}, {"$set": update})
    updated = await db.debts.find_one({"_id": obj_id(debt_id)})
    return oid(updated)

@api_router.delete("/debts/{debt_id}")
async def delete_debt(debt_id: str, current_user=Depends(require_admin)):
    result = await db.debts.delete_one({"_id": obj_id(debt_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"message": "Deleted"}



# ═══════════════════════════════════════════════════════════════════════════════
#  Expenses
# ═══════════════════════════════════════════════════════════════════════════════


class ExpenseCreate(BaseModel):
    date:         str
    category:     str
    sub_category: Optional[str] = ""
    amount:       float = Field(..., ge=0)
    description:  Optional[str] = ""
    vendor:       Optional[str] = ""
    payment_mode: Optional[str] = "Cash"
    receipt_no:   Optional[str] = ""
    notes:        Optional[str] = ""

class ExpenseUpdate(BaseModel):
    date:         Optional[str]   = None
    category:     Optional[str]   = None
    sub_category: Optional[str]   = None
    amount:       Optional[float] = Field(None, ge=0)
    description:  Optional[str]   = None
    vendor:       Optional[str]   = None
    payment_mode: Optional[str]   = None
    receipt_no:   Optional[str]   = None
    notes:        Optional[str]   = None

@api_router.get("/expenses")
async def list_expenses(
    month:    Optional[str] = Query(None),   # "2026-04"
    category: Optional[str] = Query(None),
    search:   Optional[str] = Query(None),
    limit:    int           = Query(500, ge=1, le=2000),
    current_user=Depends(verify_token),
):
    q: dict = {}
    if month:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise HTTPException(status_code=400, detail="month must be YYYY-MM")
        q["date"] = {"$regex": f"^{re.escape(month)}"}
    if category: q["category"] = category
    if search:
        s = re.escape(search)
        q["$or"] = [
            {"description": {"$regex": s, "$options": "i"}},
            {"vendor":      {"$regex": s, "$options": "i"}},
            {"category":    {"$regex": s, "$options": "i"}},
        ]
    docs = await db.expenses.find(q).sort("date", -1).limit(limit).to_list(limit)
    total = await db.expenses.count_documents(q)
    return JSONResponse(content=oids(docs), headers={"X-Total-Count": str(total)})

@api_router.post("/expenses", status_code=201)
async def create_expense(body: ExpenseCreate, current_user=Depends(verify_token)):
    doc = {
        "date":         body.date,
        "category":     body.category,
        "sub_category": body.sub_category or "",
        "amount":       body.amount,
        "description":  body.description or "",
        "vendor":       body.vendor or "",
        "payment_mode": body.payment_mode or "Cash",
        "receipt_no":   body.receipt_no or "",
        "notes":        body.notes or "",
        "created_by":   current_user.get("name", ""),
        "created_at":   utcnow().isoformat(),
    }
    result = await db.expenses.insert_one(doc)
    doc["id"] = str(result.inserted_id); doc.pop("_id", None)
    return doc

@api_router.put("/expenses/{expense_id}")
async def update_expense(expense_id: str, body: ExpenseUpdate, current_user=Depends(verify_token)):
    update = {k: v for k, v in body.dict().items() if v is not None}
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")
    await db.expenses.update_one({"_id": obj_id(expense_id)}, {"$set": update})
    updated = await db.expenses.find_one({"_id": obj_id(expense_id)})
    return oid(updated)

@api_router.delete("/expenses/{expense_id}")
async def delete_expense(expense_id: str, current_user=Depends(require_admin)):
    result = await db.expenses.delete_one({"_id": obj_id(expense_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"message": "Deleted"}

@api_router.get("/expenses/stats/summary")
async def expense_stats(
    months: int = Query(10, ge=1, le=24),
    current_user=Depends(verify_token),
):
    """Monthly expense totals by category for the last N months."""
    pipeline = [
        {"$addFields": {
            "p1": {"$dateFromString": {"dateString": {"$substr": ["$date", 0, 10]}, "format": "%Y-%m-%d", "onError": None, "onNull": None}},
            "p2": {"$dateFromString": {"dateString": "$date",                                    "format": "%d %b %Y", "onError": None, "onNull": None}},
        }},
        {"$addFields": {
            "month_key": {"$cond": [
                {"$ne": [{"$ifNull": ["$p1", "$p2"]}, None]},
                {"$dateToString": {"format": "%Y-%m", "date": {"$ifNull": ["$p1", "$p2"]}}},
                {"$substr": ["$created_at", 0, 7]},
            ]}
        }},
        {"$group": {
            "_id": {"month": "$month_key", "category": "$category"},
            "total": {"$sum": "$amount"},
        }},
        {"$sort": {"_id.month": -1}},
        {"$limit": months * len(EXPENSE_CATEGORIES)},
    ]
    rows = await db.expenses.aggregate(pipeline).to_list(500)
    # Reshape into {month: {category: total}}
    result: dict = {}
    for r in rows:
        m = r["_id"]["month"]
        c = r["_id"]["category"]
        result.setdefault(m, {})[c] = r["total"]
    return result

@api_router.get("/reports/pnl")
async def profit_and_loss(
    months: int = Query(10, ge=1, le=24),
    current_user=Depends(require_admin),
):
    """Month-by-month P&L: Revenue (sales+service+parts) minus Expenses."""
    # ── Revenue pipelines ──────────────────────────────────────────
    def month_from_sale_date():
        return {"$addFields": {
            "parsed": {"$dateFromString": {"dateString": "$sale_date", "format": "%d %b %Y", "onError": None, "onNull": None}},
        }, "$addFields": {
            "month_key": {"$cond": [
                {"$ne": ["$parsed", None]},
                {"$dateToString": {"format": "%Y-%m", "date": "$parsed"}},
                {"$substr": ["$created_at", 0, 7]},
            ]}
        }}

    sales_pipe = [
        {"$addFields": {
            "p1": {"$dateFromString": {"dateString": {"$substr": ["$sale_date", 0, 10]}, "format": "%Y-%m-%d", "onError": None, "onNull": None}},
            "p2": {"$dateFromString": {"dateString": "$sale_date",                                    "format": "%d %b %Y", "onError": None, "onNull": None}},
        }},
        {"$addFields": {"parsed": {"$ifNull": ["$p1", "$p2"]}}},
        {"$addFields": {
            "month_key": {"$cond": [
                {"$ne": ["$parsed", None]},
                {"$dateToString": {"format": "%Y-%m", "date": "$parsed"}},
                {"$substr": ["$created_at", 0, 7]},
            ]}
        }},
        {"$group": {"_id": "$month_key", "revenue": {"$sum": "$total_amount"}, "count": {"$sum": 1}}},
        {"$sort": {"_id": -1}}, {"$limit": months},
    ]
    svc_pipe = [
        {"$addFields": {"month_key": {"$substr": ["$created_at", 0, 7]}}},
        {"$group": {"_id": "$month_key", "revenue": {"$sum": "$grand_total"}}},
        {"$sort": {"_id": -1}}, {"$limit": months},
    ]
    parts_pipe = [
        {"$addFields": {"month_key": {"$substr": ["$created_at", 0, 7]}}},
        {"$group": {"_id": "$month_key", "revenue": {"$sum": "$grand_total"}}},
        {"$sort": {"_id": -1}}, {"$limit": months},
    ]
    exp_pipe = [
        {"$addFields": {"month_key": {"$substr": ["$date", 0, 7]}}},
        {"$group": {
            "_id": "$month_key",
            "total": {"$sum": "$amount"},
            "by_category": {"$push": {"cat": "$category", "amt": "$amount"}},
        }},
        {"$sort": {"_id": -1}}, {"$limit": months},
    ]

    sales_rev, svc_rev, parts_rev, exp_data = await asyncio.gather(
        db.sales.aggregate(sales_pipe).to_list(months),
        db.service_bills.aggregate(svc_pipe).to_list(months),
        db.parts_sales.aggregate(parts_pipe).to_list(months),
        db.expenses.aggregate(exp_pipe).to_list(months),
    )

    # Build month set
    all_months = sorted(set(
        [r["_id"] for r in sales_rev] +
        [r["_id"] for r in svc_rev] +
        [r["_id"] for r in parts_rev] +
        [r["_id"] for r in exp_data]
    ), reverse=True)[:months]

    def to_map(rows): return {r["_id"]: r["revenue"] for r in rows}
    sm, vm, pm = to_map(sales_rev), to_map(svc_rev), to_map(parts_rev)
    em = {r["_id"]: r["total"] for r in exp_data}
    ec = {r["_id"]: r["by_category"] for r in exp_data}

    result = []
    for m in all_months:
        sales_r = sm.get(m, 0)
        svc_r   = vm.get(m, 0)
        parts_r = pm.get(m, 0)
        revenue = sales_r + svc_r + parts_r
        expenses = em.get(m, 0)
        profit   = revenue - expenses
        margin   = round(profit / revenue * 100, 1) if revenue else 0

        # Category breakdown
        cat_map: dict = {}
        for item in ec.get(m, []):
            cat_map[item["cat"]] = cat_map.get(item["cat"], 0) + item["amt"]

        result.append({
            "month":       m,
            "sales_rev":   round(sales_r, 2),
            "service_rev": round(svc_r, 2),
            "parts_rev":   round(parts_r, 2),
            "revenue":     round(revenue, 2),
            "expenses":    round(expenses, 2),
            "profit":      round(profit, 2),
            "margin":      margin,
            "expense_by_category": cat_map,
        })
    return result
