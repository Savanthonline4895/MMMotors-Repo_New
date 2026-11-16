"""API routes: backup (split out of server.py; code moved verbatim)."""
import asyncio
import secrets
import re
from datetime import datetime
from typing import Optional
from fastapi import Depends, HTTPException, Query, Header
from fastapi.responses import JSONResponse, StreamingResponse
import os
from database import require_admin, oids, paginate_params, ist_now
from fastapi.responses import StreamingResponse
from common import _AUDIT_COLLECTIONS
from routers import api_router

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

# ═══════════════════════════════════════════════════════════════════════════════
#  Full Data Backup Export
# ═══════════════════════════════════════════════════════════════════════════════

@api_router.get("/backup/export")
async def export_backup(current_user=Depends(require_admin)):
    """
    Export all collections as a ZIP containing individual Excel files.
    One file per entity — ready to re-import via the Import page.
    """
    zip_bytes, filename = await _build_backup_zip()
    headers_resp = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "application/zip",
    }
    return StreamingResponse(iter([zip_bytes]), headers=headers_resp, media_type="application/zip")


@api_router.get("/audit-log")
async def list_audit_log(
    entity: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    p=Depends(paginate_params),
    current_user=Depends(require_admin),
):
    """Owner-only change history, newest first. search matches the bill/record number or username."""
    q: dict = {}
    if entity in _AUDIT_COLLECTIONS:
        q["entity"] = entity
    if entity_id:
        q["entity_id"] = entity_id
    if search:
        esc = re.escape(search.strip())
        q["$or"] = [{"ref": {"$regex": esc, "$options": "i"}}, {"user": {"$regex": esc, "$options": "i"}}]
    docs  = await db.audit_log.find(q).sort("at", -1).skip(p["skip"]).limit(p["limit"]).to_list(p["limit"])
    total = await db.audit_log.count_documents(q)
    return JSONResponse(content=oids(docs), headers={"X-Total-Count": str(total)})


BACKUP_KEY = os.getenv("BACKUP_KEY", "").strip()

@api_router.get("/backup/auto")
async def auto_backup(x_backup_key: str = Header("")):
    """Same ZIP as /backup/export, for the nightly Google Drive script (google-drive-backup.gs).
    Authorised by the BACKUP_KEY env var instead of a login; off when BACKUP_KEY is unset."""
    if len(BACKUP_KEY) < 32:
        raise HTTPException(status_code=404, detail="Not found")
    if not secrets.compare_digest(x_backup_key.encode(), BACKUP_KEY.encode()):
        raise HTTPException(status_code=401, detail="Bad backup key")
    zip_bytes, filename = await _build_backup_zip()
    return StreamingResponse(iter([zip_bytes]), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


async def _build_backup_zip() -> tuple[bytes, str]:
    """Build full backup ZIP. Returns (bytes, filename). Used by /backup/export."""
    import zipfile, io as _io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    GOLD = 'FFB8860B'; WHITE = 'FFFFFFFF'

    def make_sheet(wb, title, rows, headers):
        ws = wb.create_sheet(title)
        for ci, h in enumerate(headers, 1):
            c = ws.cell(row=1, column=ci, value=h)
            c.font      = Font(name='Arial', bold=True, color=WHITE, size=9)
            c.fill      = PatternFill('solid', start_color=GOLD)
            c.alignment = Alignment(horizontal='center', vertical='center')
        for ri, row in enumerate(rows, 2):
            for ci, h in enumerate(headers, 1):
                val = row.get(h, '')
                if isinstance(val, (dict, list)):
                    val = str(val)
                ws.cell(row=ri, column=ci, value=val)
        for col in ws.columns:
            max_len = max((len(str(c.value or '')) for c in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)
        return ws

    # ── Fetch all collections ──────────────────────────────────────────────────
    (
        customers, vehicles, sales, service_jobs, parts, expenses, debts, staff,
        service_bills, parts_bills, purchase_bills, vendors, bookings,
    ) = await asyncio.gather(
        db.customers.find({}).to_list(100000),
        db.vehicles.find({}).to_list(100000),
        db.sales.find({}).to_list(100000),
        db.service_jobs.find({}).to_list(100000),
        db.spare_parts.find({}).to_list(100000),
        db.expenses.find({}).to_list(100000),
        db.debts.find({}).to_list(100000),
        db.users.find({}, {"password": 0}).to_list(1000),
        db.service_bills.find({}).to_list(100000),
        db.parts_bills.find({}).to_list(100000),
        db.purchase_bills.find({}).to_list(100000),
        db.vendors.find({}).to_list(100000),
        db.vehicle_bookings.find({}).to_list(100000),
    )

    # Convert ObjectIds to strings
    def clean(docs):
        out = []
        for d in docs:
            row = {}
            for k, v in d.items():
                if k == '_id': row['_id'] = str(v)
                elif hasattr(v, '__str__') and 'ObjectId' in type(v).__name__: row[k] = str(v)
                elif isinstance(v, datetime): row[k] = v.isoformat()
                elif isinstance(v, dict): row[k] = str(v)
                elif isinstance(v, list): row[k] = ', '.join(str(x) for x in v)
                else: row[k] = v
            out.append(row)
        return out

    customers_c    = clean(customers)
    vehicles_c     = clean(vehicles)
    sales_c        = clean(sales)
    service_c      = clean(service_jobs)
    parts_c        = clean(parts)
    expenses_c     = clean(expenses)
    debts_c        = clean(debts)
    staff_c        = clean(staff)

    # ── Build separate Excel files ─────────────────────────────────────────────
    def make_workbook(rows, headers, sheet_name):
        wb = Workbook(); wb.remove(wb.active)
        make_sheet(wb, sheet_name, rows, headers)
        buf = _io.BytesIO(); wb.save(buf); buf.seek(0)
        return buf.read()

    today = ist_now().strftime("%Y-%m-%d")

    files = [
        (f"customers_{today}.xlsx",    make_workbook(customers_c, ['name','mobile','care_of','email','address','tags','created_at'], 'Customers')),
        (f"vehicles_{today}.xlsx",     make_workbook(vehicles_c,  ['brand','model','variant','color','chassis_number','engine_number','vehicle_number','key_number','type','status','inbound_date','inbound_location','return_date','returned_location','customer_name','customer_mobile','created_at'], 'Vehicles')),
        (f"sales_{today}.xlsx",        make_workbook(sales_c,     ['invoice_number','sale_date','customer_name','customer_mobile','care_of','customer_address','vehicle_brand','vehicle_model','vehicle_variant','vehicle_color','vehicle_number','chassis_number','engine_number','sale_price','discount','insurance','rto','total_amount','payment_mode','nominee','status','created_at'], 'Sales')),
        (f"service_{today}.xlsx",      make_workbook(service_c,   ['job_number','check_in_date','customer_name','customer_mobile','vehicle_number','brand','model','odometer_km','complaint','technician','status','grand_total','estimated_delivery','notes','created_at'], 'Service')),
        (f"spare_parts_{today}.xlsx",  make_workbook(parts_c,     ['part_number','name','category','brand','compatible_with','stock','reorder_level','purchase_price','selling_price','gst_rate','hsn_code','location','created_at'], 'Parts')),
        (f"expenses_{today}.xlsx",     make_workbook(expenses_c,  ['date','category','sub_category','amount','description','vendor','payment_mode','receipt_no','notes','created_by','created_at'], 'Expenses')),
        (f"debts_{today}.xlsx",        make_workbook(debts_c,     ['customer_name','customer_mobile','amount','paid','balance','description','due_date','status','source','created_at'], 'Debts')),
        # Bills keep their line items as text in the "items" column
        (f"service_bills_{today}.xlsx", make_workbook(clean(service_bills), ['bill_number','bill_date','job_number','customer_name','customer_mobile','customer_gstin','vehicle_number','items','labour_charges','subtotal','gst_total','discount','net_total','payment_mode','notes','created_at'], 'Service Bills')),
        (f"parts_bills_{today}.xlsx",   make_workbook(clean(parts_bills),   ['bill_number','bill_date','customer_name','customer_mobile','customer_vehicle','items','subtotal','gst_total','discount','net_total','grand_total','payment_mode','created_at'], 'Parts Bills')),
        (f"purchase_bills_{today}.xlsx", make_workbook(clean(purchase_bills), ['bill_number','bill_date','vendor_name','vendor_gstin','items','subtotal','total_cgst','total_sgst','round_off','grand_total','notes','created_at'], 'Purchase Bills')),
        (f"vendors_{today}.xlsx",       make_workbook(clean(vendors),       ['name','gstin','phone','email','address','notes','created_at'], 'Vendors')),
        (f"vehicle_bookings_{today}.xlsx", make_workbook(clean(bookings),   ['booking_number','booking_date','customer_name','customer_mobile','brand','model','variant','color','advance_amount','payment_mode','expected_date','status','invoice_number','refund_amount','cancel_reason','notes','created_at'], 'Bike Bookings')),
        (f"staff_{today}.xlsx",        make_workbook(staff_c,     ['name','username','mobile','email','role','salary','join_date','status','allowed_pages','created_at'], 'Staff')),
    ]

    # ── Pack into ZIP ──────────────────────────────────────────────────────────
    zip_buf = _io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname, data in files:
            zf.writestr(f"MMMotors_Backup_{today}/{fname}", data)

    zip_buf.seek(0)
    return zip_buf.read(), f"MMMotors_Backup_{today}.zip"
