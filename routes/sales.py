"""API routes: sales (split out of server.py; code moved verbatim)."""
import re
from typing import Optional
from fastapi import Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
import os
import io
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas
import database as _db
from database import verify_token, require_admin, next_sequence, _sync_counter, oid, oids, obj_id, utcnow, ist_now, amount_in_words
import io
from fastapi.responses import StreamingResponse
from common import SALE_MILESTONE_KEYS, SHEETS_WEBHOOK_SECRET, SHEETS_WEBHOOK_URL, _register_fonts, _sheet_sync, default_milestones
from models import FittingsFittedUpdate, MilestoneUpdate, SaleCreate, SaleUpdate
from routers import api_router
from routes.service_bills import _deduct_fittings, _priority_low_alerts, _restore_fittings

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

# ═══════════════════════════════════════════════════════════════════════════════
#  PDF INVOICE GENERATOR
# ═══════════════════════════════════════════════════════════════════════════════

def _generate_sale_pdf(sale: dict) -> bytes:
    _register_fonts()
    GOLD        = colors.HexColor('#B8860B')
    GOLD_LIGHT  = colors.HexColor('#F5E6C0')
    DARK        = colors.HexColor('#1A1A1A')
    MID         = colors.HexColor('#4A4A4A')
    DIM         = colors.HexColor('#8A8A8A')
    RULE        = colors.HexColor('#D0C090')
    STRIPE      = colors.HexColor('#F7F7F4')
    WHITE       = colors.white
    PAGE_BG     = colors.HexColor('#FDFCF8')
    RS          = 'Rs.'
    W, H = A4
    ML = 16*mm; MR = W - 16*mm; TW = MR - ML
    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    c.setFillColor(PAGE_BG); c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setFillColor(DARK); c.rect(0, H - 5*mm, W, 5*mm, fill=1, stroke=0)
    c.setFillColor(GOLD); c.rect(0, H - 6.5*mm, W, 1.5*mm, fill=1, stroke=0)
    HEADER_Y = H - 30*mm
    logo_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'mm_logo.png')  # repo root
    if os.path.exists(logo_path):
        LOGO_SIZE = 30*mm
        c.drawImage(logo_path, ML, HEADER_Y - 10*mm, width=LOGO_SIZE, height=LOGO_SIZE, preserveAspectRatio=True, mask='auto')
        name_x = ML + 30*mm
    else:
        name_x = ML
    c.setFont('Sans-Bold', 20); c.setFillColor(DARK); c.drawString(name_x, HEADER_Y + 4*mm, 'MM MOTORS')
    c.setFont('Sans', 7); c.setFillColor(DIM); c.drawString(name_x, HEADER_Y - 2*mm, 'MULTI-BRAND DEALERSHIP  ·  MALUR')
    c.setFont('Mono-Bold', 8); c.setFillColor(GOLD); c.drawRightString(MR, HEADER_Y + 5*mm, 'SALE  INVOICE')
    c.setFont('Mono-Bold', 13); c.setFillColor(DARK); c.drawRightString(MR, HEADER_Y - 2*mm, sale.get('invoice_number', '—'))
    c.setFont('Sans', 7.5); c.setFillColor(DIM); c.drawRightString(MR, HEADER_Y - 7*mm, f"Date: {sale.get('sale_date', '—')}")
    DIV_Y = HEADER_Y - 12*mm
    c.setStrokeColor(GOLD); c.setLineWidth(0.8); c.line(ML, DIV_Y, MR, DIV_Y)
    def sec_label(x, y, text):
        c.setFont('Sans-Bold', 6.5); c.setFillColor(GOLD); c.drawString(x, y, text.upper())
        c.setStrokeColor(GOLD); c.setLineWidth(0.35); c.line(x, y - 1*mm, x + 38*mm, y - 1*mm)
    def irow(x, y, label, val, mono=False, max_w=None):
        c.setFont('Sans', 7); c.setFillColor(DIM); c.drawString(x, y, label)
        c.setFont('Mono' if mono else 'Sans-Bold', 7); c.setFillColor(DARK)
        text = str(val) if val else '—'
        # Strip rupee unicode — use "Rs." instead so Liberation Sans renders it
        text = text.replace('₹', 'Rs.')
        if max_w:
            # Truncate with ellipsis if text exceeds column width
            while c.stringWidth(text, 'Mono' if mono else 'Sans-Bold', 7) > max_w and len(text) > 4:
                text = text[:-2] + '…'
        c.drawString(x + 24*mm, y, text)
    def irow_wrap(x, y, label, val, max_w, line_h=4.5*mm):
        """Like irow but wraps long values across multiple lines."""
        c.setFont('Sans', 7); c.setFillColor(DIM); c.drawString(x, y, label)
        c.setFont('Sans-Bold', 7); c.setFillColor(DARK)
        text = str(val) if val else '—'
        words = text.split()
        line = ''; lines = []
        for w in words:
            test = (line + ' ' + w).strip()
            if c.stringWidth(test, 'Sans-Bold', 7) <= max_w:
                line = test
            else:
                if line: lines.append(line)
                line = w
        if line: lines.append(line)
        for li, ln in enumerate(lines):
            c.drawString(x + 24*mm, y - li * line_h, ln)
        return len(lines)
    COL1 = ML; COL2 = W/2 + 2*mm; RH = 5*mm
    COL1_MAX = (W/2 - 2*mm) - (ML + 24*mm)   # max text width for COL1 values
    COL2_MAX = MR - (COL2 + 24*mm)            # max text width for COL2 values
    INFO_Y = DIV_Y - 6*mm
    nominee = sale.get('nominee', {}) or {}
    sec_label(COL1, INFO_Y, 'Customer Details')
    # Address uses wrap; others use single-line with truncation guard
    irow(COL1, INFO_Y - 1.6*RH, 'Name',    sale.get('customer_name',''),   max_w=COL1_MAX)
    irow(COL1, INFO_Y - 2.6*RH, 'C/O',     sale.get('care_of',''),          max_w=COL1_MAX)
    irow(COL1, INFO_Y - 3.6*RH, 'Mobile',  sale.get('customer_mobile',''),  max_w=COL1_MAX)
    addr_lines = irow_wrap(COL1, INFO_Y - 4.6*RH, 'Address', sale.get('customer_address',''), max_w=COL1_MAX)
    addr_extra = max(0, addr_lines - 1)
    irow(COL1, INFO_Y - (5.6 + addr_extra)*RH, 'Payment', sale.get('payment_mode',''), max_w=COL1_MAX)
    sec_label(COL2, INFO_Y, 'Vehicle Details')
    for i, (l, v) in enumerate([('Brand', sale.get('vehicle_brand','')), ('Model', sale.get('vehicle_model','')), ('Variant', sale.get('vehicle_variant','')), ('Colour', sale.get('vehicle_color','')), ('Financier', sale.get('financier',''))]):
        irow(COL2, INFO_Y - (i+1.6)*RH, l, v, max_w=COL2_MAX)
    SEC2_Y = INFO_Y - (7.8 + addr_extra)*RH   # extra row for wrapped address
    sec_label(COL1, SEC2_Y, 'Registration / Chassis')
    for i, (l, v, m) in enumerate([
        ('Vehicle No', sale.get('vehicle_number',''), False),
        ('RTO',        sale.get('rto',''),            False),
        ('Chassis No', sale.get('chassis_number',''), True),
        ('Engine No',  sale.get('engine_number',''),  True),
    ]):
        irow(COL1, SEC2_Y - (i+1.6)*RH, l, v, mono=m, max_w=COL1_MAX)
    sec_label(COL2, SEC2_Y, 'Nominee (Insurance)')
    for i, (l, v) in enumerate([('Name', nominee.get('name','')), ('Relation', nominee.get('relation','')), ('Age', nominee.get('age','')), ('Mobile', nominee.get('number',''))]):
        irow(COL2, SEC2_Y - (i+1.6)*RH, l, v)
    TBL_Y = SEC2_Y - 5.8*RH; TBL_TY = TBL_Y + 1*mm
    c.setFillColor(DARK); c.rect(ML, TBL_TY - 6.5*mm, TW, 6.5*mm, fill=1, stroke=0)
    c.setFont('Sans-Bold', 7); c.setFillColor(WHITE)
    TC = [ML+3*mm, ML+65*mm, ML+100*mm, ML+128*mm, MR-3*mm]
    c.drawString(TC[0], TBL_TY - 4.5*mm, 'DESCRIPTION'); c.drawString(TC[1], TBL_TY - 4.5*mm, 'CHASSIS / DETAILS')
    c.drawString(TC[2], TBL_TY - 4.5*mm, 'PAYMENT'); c.drawString(TC[3], TBL_TY - 4.5*mm, 'MODE')
    c.drawRightString(TC[4], TBL_TY - 4.5*mm, 'AMOUNT')
    DR_Y = TBL_TY - 14*mm
    c.setFillColor(STRIPE); c.rect(ML, DR_Y - 1*mm, TW, 9*mm, fill=1, stroke=0)
    c.setFont('Sans-Bold', 8); c.setFillColor(DARK)
    c.drawString(TC[0], DR_Y + 4*mm, f"{sale.get('vehicle_brand','')} {sale.get('vehicle_model','')}")
    c.setFont('Sans', 7); c.setFillColor(DIM)
    c.drawString(TC[0], DR_Y + 0.8*mm, f"{sale.get('vehicle_variant','')}  ·  {sale.get('vehicle_color','')}")
    c.setFont('Mono', 7); c.setFillColor(DARK); c.drawString(TC[1], DR_Y + 2.5*mm, sale.get('chassis_number',''))
    c.setFont('Sans', 7.5); c.drawString(TC[2], DR_Y + 2.5*mm, sale.get('payment_mode','')); c.drawString(TC[3], DR_Y + 2.5*mm, f"Adv. {RS}{int(sale['booking_advance']):,} recd" if sale.get('booking_advance') else 'Full Payment')
    total = sale.get('total_amount') or sale.get('sale_price') or 0
    total_str = f"{int(total):,}" if isinstance(total, (int, float)) else str(total)
    c.setFont('Sans-Bold', 10); c.setFillColor(GOLD); c.drawRightString(TC[4], DR_Y + 2.5*mm, f"{RS}{total_str}")
    c.setStrokeColor(GOLD); c.setLineWidth(0.7); c.line(ML, DR_Y - 2*mm, MR, DR_Y - 2*mm)
    TOT_Y = DR_Y - 12*mm
    c.setFont('Sans-Italic', 7); c.setFillColor(DIM); c.drawString(ML, TOT_Y + 4*mm, sale.get('amount_in_words',''))
    if sale.get('booking_advance'):   # advance taken on a bike booking; the invoice total stays the full price
        adv = float(sale['booking_advance'])
        c.setFont('Sans-Bold', 7.5); c.setFillColor(DARK)
        c.drawString(ML, TOT_Y - 0.5*mm, f"Advance received {RS}{int(adv):,} ({sale.get('booking_number','')})  ·  "
                                         f"Balance payable {RS}{int(max(float(total or 0) - adv, 0)):,}")
    c.setFillColor(GOLD_LIGHT); c.setStrokeColor(GOLD); c.setLineWidth(0.6)
    c.roundRect(MR - 58*mm, TOT_Y - 1*mm, 58*mm, 11*mm, 1.5*mm, fill=1, stroke=1)
    c.setFont('Sans', 7); c.setFillColor(MID); c.drawString(MR - 55*mm, TOT_Y + 5.5*mm, 'TOTAL AMOUNT')
    c.setFont('Sans-Bold', 13); c.setFillColor(DARK); c.drawRightString(MR - 3*mm, TOT_Y + 4*mm, f"{RS}{total_str}")
    SIG_Y = TOT_Y - 14*mm
    c.setStrokeColor(RULE); c.setLineWidth(0.5)
    c.line(ML, SIG_Y, ML + 52*mm, SIG_Y)
    c.setFont('Sans', 6.5); c.setFillColor(DIM); c.drawString(ML, SIG_Y - 4*mm, "Customer's Signature")
    c.setFont('Sans-Bold', 7); c.setFillColor(DARK); c.drawString(ML, SIG_Y - 8.5*mm, sale.get('customer_name','').upper())
    c.setFont('Sans', 6.5); c.setFillColor(DIM); c.drawCentredString(W/2, SIG_Y - 4*mm, f"Sold by: {sale.get('sold_by','MM Motors')}")
    c.line(MR - 52*mm, SIG_Y, MR, SIG_Y)
    c.setFont('Sans', 6.5); c.setFillColor(DIM); c.drawRightString(MR, SIG_Y - 4*mm, 'Authorised Signatory')
    c.setFont('Sans-Bold', 7); c.setFillColor(DARK); c.drawRightString(MR, SIG_Y - 8.5*mm, 'MM MOTORS')
    SCHED_Y = SIG_Y - 20*mm
    c.setFillColor(DARK); c.rect(ML, SCHED_Y, TW, 6.5*mm, fill=1, stroke=0)
    c.setFillColor(GOLD); c.rect(ML, SCHED_Y + 6.5*mm, TW, 0.8*mm, fill=1, stroke=0)
    c.setFont('Sans-Bold', 8.5); c.setFillColor(WHITE); c.drawString(ML + 4*mm, SCHED_Y + 2*mm, 'SERVICE SCHEDULE')
    DEAR_Y = SCHED_Y - 13.5*mm
    c.setFillColor(GOLD_LIGHT); c.setStrokeColor(GOLD); c.setLineWidth(0.5)
    c.roundRect(ML, DEAR_Y, TW, 12*mm, 1*mm, fill=1, stroke=1)
    c.setFont('Sans-Bold', 7.5); c.setFillColor(DARK); c.drawString(ML + 3*mm, DEAR_Y + 8*mm, 'DEAR VALUED CUSTOMER,')
    c.setFont('Sans', 7); c.setFillColor(MID)
    c.drawString(ML + 3*mm, DEAR_Y + 4.5*mm, 'We thank you for choosing our world-class vehicle. To ensure optimal performance and longevity,')
    c.drawString(ML + 3*mm, DEAR_Y + 1.5*mm, 'please follow the service schedule below for a pleasant riding experience at all times.')
    TH_Y = DEAR_Y - 8*mm
    c.setFillColor(DARK); c.rect(ML, TH_Y, TW, 6.5*mm, fill=1, stroke=0)
    c.setFont('Sans-Bold', 7); c.setFillColor(WHITE)
    SC1 = ML + 3*mm; SC2 = ML + 52*mm; SC3 = ML + 115*mm
    c.drawString(SC1, TH_Y + 2*mm, 'SERVICE DATE'); c.drawString(SC2, TH_Y + 2*mm, 'SERVICE TYPE'); c.drawString(SC3, TH_Y + 2*mm, 'RECOMMENDED SCHEDULE')
    SERVICES = [('FIRST SERVICE','500-700 kms or 15-30 days'),('SECOND SERVICE','3000-3500 kms or 30-90 days'),('THIRD SERVICE','6000-6500 kms or 90-180 days'),('FOURTH SERVICE','9000-9500 kms or 180-270 days')]
    SRH = 6.5*mm
    for i, (stype, sched) in enumerate(SERVICES):
        ry = TH_Y - (i + 1) * SRH
        c.setFillColor(WHITE if i % 2 == 0 else STRIPE); c.rect(ML, ry, TW, SRH, fill=1, stroke=0)
        c.setStrokeColor(RULE); c.setLineWidth(0.3); c.line(ML, ry, MR, ry)
        c.setFont('Mono', 7); c.setFillColor(DIM); c.drawString(SC1, ry + 2*mm, '__/__/____')
        c.setFont('Sans-Bold', 7); c.setFillColor(GOLD); c.drawString(SC2, ry + 2*mm, stype)
        c.setFont('Sans', 7); c.setFillColor(DARK); c.drawString(SC3, ry + 2*mm, sched)
    c.setStrokeColor(RULE); c.setLineWidth(0.5)
    c.rect(ML, TH_Y - 4*SRH - 1, TW, 6.5*mm + 13.5*mm + 8*mm + 4*SRH + 1, fill=0, stroke=1)
    NOTE_Y = TH_Y - 4*SRH - 6.5*mm
    c.setFillColor(GOLD_LIGHT); c.setStrokeColor(GOLD); c.setLineWidth(0.5)
    c.roundRect(ML, NOTE_Y - 0.5*mm, TW, 6*mm, 1*mm, fill=1, stroke=1)
    c.setFont('Sans-Bold', 7.5); c.setFillColor(colors.HexColor('#7A5800'))
    c.drawCentredString(W/2, NOTE_Y + 1.8*mm, 'IMPORTANT: Follow whichever milestone comes first (km or days)')
    THANKS_Y = NOTE_Y - 17*mm
    c.setFillColor(STRIPE); c.setStrokeColor(RULE); c.setLineWidth(0.5)
    c.roundRect(ML, THANKS_Y - 1*mm, TW, 28*mm, 2*mm, fill=1, stroke=1)
    c.setFillColor(GOLD); c.rect(ML, THANKS_Y + 26*mm, TW, 1.5*mm, fill=1, stroke=0)
    for label, x in [('* Trusted Dealer', ML + 6*mm), ('* 24/7 Service Support', W/2 - 22*mm), ('* Quality Guaranteed', MR - 50*mm)]:
        c.setFont('Sans', 7); c.setFillColor(MID); c.drawString(x, THANKS_Y + 20.5*mm, label)
    c.setStrokeColor(RULE); c.setLineWidth(0.4); c.line(ML + 4*mm, THANKS_Y + 18.5*mm, MR - 4*mm, THANKS_Y + 18.5*mm)
    c.setFont('Sans-Bold', 12); c.setFillColor(DARK); c.drawCentredString(W/2, THANKS_Y + 13*mm, 'Thank You for Choosing M M Motors!')
    c.setFont('Sans-Italic', 7.5); c.setFillColor(DIM); c.drawCentredString(W/2, THANKS_Y + 8.5*mm, 'Your trust drives our excellence in two-wheeler sales and service.')
    c.setFont('Sans', 7.5); c.setFillColor(MID); c.drawCentredString(W/2, THANKS_Y + 4.5*mm, '* Premium Quality   *  Expert Service   *  Customer First')
    c.setFillColor(DARK); c.rect(0, 0, W, 7.5*mm, fill=1, stroke=0)
    c.setFillColor(GOLD); c.rect(0, 7.5*mm, W, 1*mm, fill=1, stroke=0)
    c.setFont('Sans', 6); c.setFillColor(colors.HexColor('#888888'))
    c.drawString(ML, 2.8*mm, 'This is a computer-generated document. No signature required if digitally authenticated.')
    c.drawRightString(MR, 2.8*mm, 'MM Motors  ·  Malur  ·  Multi-brand Dealership')
    c.save(); buf.seek(0)
    return buf.read()


# ═══════════════════════════════════════════════════════════════════════════════
#  SALES
# ═══════════════════════════════════════════════════════════════════════════════

@api_router.get("/sales")
async def list_sales(
    customer_id: Optional[str] = Query(None),
    status:      Optional[str] = Query(None),
    from_date:   Optional[str] = Query(None),
    to_date:     Optional[str] = Query(None),
    search:      Optional[str] = Query(None),
    limit:       int           = Query(300, ge=1, le=5000),
    current_user=Depends(verify_token),
):
    query: dict = {}
    if customer_id: query["customer_id"] = customer_id
    if status:      query["status"]      = status
    if search:
        s = re.escape(search)
        query["$or"] = [
            {"invoice_number":  {"$regex": s, "$options": "i"}},
            {"customer_name":   {"$regex": s, "$options": "i"}},
            {"customer_mobile": {"$regex": s, "$options": "i"}},
            {"vehicle_model":   {"$regex": s, "$options": "i"}},
            {"vehicle_number":  {"$regex": s, "$options": "i"}},
            {"chassis_number":  {"$regex": s, "$options": "i"}},
            {"engine_number":   {"$regex": s, "$options": "i"}},
        ]
    docs  = await db.sales.find(query).sort("created_at", -1).limit(limit).to_list(limit)
    total = await db.sales.count_documents(query)
    return JSONResponse(content=oids(docs), headers={"X-Total-Count": str(total)})

@api_router.post("/sales", status_code=201)
async def create_sale(body: SaleCreate, current_user=Depends(verify_token)):
    customer = await db.customers.find_one({"_id": obj_id(body.customer_id)})
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    vehicle = await db.vehicles.find_one({"_id": obj_id(body.vehicle_id)})
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    if vehicle.get("status") == "sold":
        raise HTTPException(status_code=409, detail="Vehicle already sold")
    booking = None
    if body.booking_id:
        booking = await db.vehicle_bookings.find_one({"_id": obj_id(body.booking_id)})
        if not booking or booking.get("status") not in ("waiting", "arrived"):
            raise HTTPException(status_code=409, detail="That bike booking is already closed or no longer exists")
    total_amount = body.total_amount if body.total_amount is not None else (body.sale_price or 0)
    inv_no    = await next_sequence("invoice")
    sale_date = body.sale_date or ist_now().strftime("%d %b %Y")
    doc = {
        "invoice_number":  inv_no,
        "customer_id":     body.customer_id,
        "customer_name":   customer["name"],
        "customer_mobile": customer.get("mobile",""),
        "customer_address":customer.get("address",""),
        "customer_gstin":  (customer.get("gstin") or "").strip().upper(),  # GSTR-1 routes B2B on this
        "care_of":         body.care_of or "",
        "vehicle_id":      body.vehicle_id,
        "vehicle_brand":   body.vehicle_brand   if body.vehicle_brand   is not None else vehicle["brand"],
        "vehicle_model":   body.vehicle_model   if body.vehicle_model   is not None else vehicle["model"],
        "vehicle_variant": body.vehicle_variant if body.vehicle_variant is not None else vehicle.get("variant",""),
        "vehicle_color":   body.vehicle_color   if body.vehicle_color   is not None else vehicle.get("color",""),
        "chassis_number":  body.chassis_number  if body.chassis_number  is not None else vehicle.get("chassis_number",""),
        "engine_number":   body.engine_number   if body.engine_number   is not None else vehicle.get("engine_number",""),
        "vehicle_number":  body.vehicle_number or vehicle.get("vehicle_number",""),
        "rto":             body.rto or "",
        "sale_price":      body.sale_price,
        "total_amount":    round(total_amount, 2),
        "amount_in_words": amount_in_words(total_amount),
        "finance_type":    body.finance_type or "cash",
        "financier":       body.financier or "",
        "loan_amount":     body.loan_amount or 0,
        "nominee":         body.nominee.dict() if body.nominee else {},
        "payment_mode":    body.payment_mode or "Cash",
        "sold_by":         body.sold_by or current_user.get("name",""),
        "sale_date":       sale_date,
        "status":          "pending",
        "notes":           body.notes or "",
        "hsrp_front":      body.hsrp_front or "",
        "hsrp_back":       body.hsrp_back or "",
        "hsrp_front_id":   body.hsrp_front_id or "",
        "hsrp_back_id":    body.hsrp_back_id or "",
        "hsrp_date":       body.hsrp_date or "",
        "hsrp_notes":      body.hsrp_notes or "",
        "milestones":      {**default_milestones(), **(body.milestones or {})},
        "milestone_dates": dict(body.milestone_dates or {}),
        "created_at":      utcnow().isoformat(),
    }
    if booking:
        doc.update({"booking_id": body.booking_id, "booking_number": booking.get("booking_number", ""),
                    "booking_advance": float(booking.get("advance_amount") or 0)})
    # Fitting stock + sale + vehicle status go together, so a failure cannot leave
    # accessories deducted against a sale that was never written.
    inserted_id = None
    touched: dict = {}
    async with await _db.client.start_session() as session:
        async def _txn(s):
            nonlocal inserted_id
            touched.clear()   # a retried transaction must not double-count
            items_out, t, pending = await _deduct_fittings(body.fittings, inv_no, session=s)
            touched.update(t)
            doc["fittings"]         = items_out
            doc["pending_fittings"] = pending
            result = await db.sales.insert_one(doc, session=s)
            inserted_id = result.inserted_id
            await db.vehicles.update_one(
                {"_id": obj_id(body.vehicle_id)},
                {"$set": {"status": "sold", "sold_date": sale_date, "invoice_number": inv_no}},
                session=s,
            )
            if booking:   # close the booking with the sale, or neither happens
                await db.vehicle_bookings.update_one(
                    {"_id": booking["_id"], "status": {"$in": ["waiting", "arrived"]}},
                    {"$set": {"status": "delivered", "invoice_number": inv_no, "sale_id": str(inserted_id),
                              "delivered_at": utcnow().isoformat()}},
                    session=s,
                )
        await session.with_transaction(_txn)

    doc["low_stock_alerts"] = await _priority_low_alerts(touched)
    # Sheet copy, after the sale is safely committed. _sheet_sync stamps the outcome on
    # the sale, so a failure shows as the "not in sheet" marker rather than a lost row.
    sheet = await _sheet_sync({**doc, "_id": inserted_id})
    doc["id"] = str(inserted_id); doc.pop("_id", None)
    doc["sheet_ok"] = bool(sheet.get("ok") or sheet.get("skipped"))
    return doc

@api_router.get("/sales/stats/summary")
async def sales_stats(current_user=Depends(verify_token)):
    today = ist_now().strftime("%d %b %Y")
    pipeline_total = [{"$group": {"_id": None, "total": {"$sum": "$total_amount"}, "count": {"$sum": 1}}}]
    result = await db.sales.aggregate(pipeline_total).to_list(1)
    all_time    = result[0] if result else {"total": 0, "count": 0}
    today_sales = await db.sales.count_documents({"sale_date": today})
    pending     = await db.sales.count_documents({"status": "pending"})
    return {"total_count": all_time.get("count",0), "total_revenue": all_time.get("total",0), "today_count": today_sales, "pending_delivery": pending}

@api_router.get("/sales/{sale_id}")
async def get_sale(sale_id: str, current_user=Depends(verify_token)):
    doc = await db.sales.find_one({"_id": obj_id(sale_id)})
    if not doc:
        doc = await db.sales.find_one({"invoice_number": sale_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Sale not found")
    return oid(doc)

@api_router.get("/sales/{sale_id}/pdf")
async def sale_pdf(sale_id: str, current_user=Depends(verify_token)):
    doc = await db.sales.find_one({"_id": obj_id(sale_id)})
    if not doc:
        doc = await db.sales.find_one({"invoice_number": sale_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Sale not found")
    sale = oid(doc)
    try:
        pdf_bytes = _generate_sale_pdf(sale)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")
    invoice_no = sale.get("invoice_number","invoice").replace("/","-")
    return StreamingResponse(
        io.BytesIO(pdf_bytes), media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=MM_Motors_{invoice_no}.pdf"},
    )

@api_router.put("/sales/{sale_id}")
async def update_sale(sale_id: str, body: SaleUpdate, current_user=Depends(verify_token)):
    sale = await db.sales.find_one({"_id": obj_id(sale_id)})
    if not sale:
        raise HTTPException(status_code=404, detail="Sale not found")
    update: dict = {}
    for field in ("status","delivery_date","vehicle_number","rto","payment_mode","notes","customer_name","customer_mobile","customer_address","care_of","total_amount","sale_price","finance_type","financier","loan_amount","sale_date","sold_by","hsrp_front","hsrp_back","hsrp_front_id","hsrp_back_id","hsrp_date","hsrp_notes","vehicle_brand","vehicle_model","vehicle_variant","vehicle_color","chassis_number","engine_number"):
        val = getattr(body, field)
        if val is not None:
            update[field] = val
    if "total_amount" in update:
        update["amount_in_words"] = amount_in_words(update["total_amount"])
    if body.nominee is not None:
        update["nominee"] = body.nominee.dict()
    if body.vehicle_id is not None and body.vehicle_id != sale.get("vehicle_id",""):
        if current_user.get("role") != "owner":
            raise HTTPException(status_code=403, detail="Only owner can change vehicle on a sale")
        new_vehicle = await db.vehicles.find_one({"_id": obj_id(body.vehicle_id)})
        if not new_vehicle:
            raise HTTPException(status_code=404, detail="New vehicle not found")
        if new_vehicle.get("status") == "sold" and str(new_vehicle["_id"]) != sale.get("vehicle_id",""):
            raise HTTPException(status_code=409, detail="Vehicle already sold")
        if sale.get("vehicle_id"):
            await db.vehicles.update_one({"_id": obj_id(sale["vehicle_id"])}, {"$set": {"status": "in_stock"}, "$unset": {"sold_date":"","invoice_number":""}})
        await db.vehicles.update_one({"_id": obj_id(body.vehicle_id)}, {"$set": {"status":"sold","sold_date":sale.get("sale_date",""),"invoice_number":sale.get("invoice_number","")}})
        update["vehicle_id"]      = body.vehicle_id
        # Use manually-edited values if provided, otherwise fall back to new vehicle's DB values
        update.setdefault("vehicle_brand",   new_vehicle.get("brand",""))
        update.setdefault("vehicle_model",   new_vehicle.get("model",""))
        update.setdefault("vehicle_variant", new_vehicle.get("variant",""))
        update.setdefault("vehicle_color",   new_vehicle.get("color",""))
        update.setdefault("chassis_number",  new_vehicle.get("chassis_number",""))
        update.setdefault("engine_number",   new_vehicle.get("engine_number",""))
    if body.fittings is None:
        if not update:
            raise HTTPException(status_code=400, detail="Nothing to update")
        await db.sales.update_one({"_id": obj_id(sale_id)}, {"$set": update})
        fresh = await db.sales.find_one({"_id": obj_id(sale_id)})
        await _sheet_sync(fresh)
        return oid(await db.sales.find_one({"_id": obj_id(sale_id)}))

    # Fittings changed: put back exactly what was deducted, then take the new set,
    # both inside one transaction so a half-applied edit cannot survive.
    inv_no  = sale.get("invoice_number", "")
    touched: dict = {}
    async with await _db.client.start_session() as session:
        async def _txn(s):
            touched.clear()   # a retried transaction must not double-count
            await _restore_fittings(sale.get("fittings", []), inv_no, session=s)
            items_out, t, pending = await _deduct_fittings(body.fittings, inv_no, session=s,
                                                           reason="sale_fitting_edit")
            touched.update(t)
            upd = dict(update)
            upd["fittings"]         = items_out
            upd["pending_fittings"] = pending
            await db.sales.update_one({"_id": obj_id(sale_id)}, {"$set": upd}, session=s)
        await session.with_transaction(_txn)

    await _sheet_sync(await db.sales.find_one({"_id": obj_id(sale_id)}))
    payload = oid(await db.sales.find_one({"_id": obj_id(sale_id)}))
    payload["low_stock_alerts"] = await _priority_low_alerts(touched)
    return payload


@api_router.post("/sales/{sale_id}/sheet-sync")
async def resync_sale_to_sheet(sale_id: str, current_user=Depends(verify_token)):
    """Retry the Google Sheet copy for one sale. Used by the 'not in sheet' marker on
    the sales list after a network blip or a misconfigured deployment."""
    if not SHEETS_WEBHOOK_URL or not SHEETS_WEBHOOK_SECRET:
        raise HTTPException(status_code=400, detail="Google Sheet sync is not configured on the server")
    sale = await db.sales.find_one({"_id": obj_id(sale_id)})
    if not sale:
        raise HTTPException(status_code=404, detail="Sale not found")
    result = await _sheet_sync(sale)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=f"Sheet rejected the row: {result.get('error', 'unknown error')}")
    return oid(await db.sales.find_one({"_id": obj_id(sale_id)}))


@api_router.post("/sales/{sale_id}/fittings-fitted")
async def mark_fittings_fitted(sale_id: str, body: FittingsFittedUpdate, current_user=Depends(verify_token)):
    """Clear the pending-fitting marker once the accessories are on the bike.

    Finishes the stock deduction that could not happen at sale time — by now the parts
    have usually arrived on a purchase bill. If stock still is not there, the marker is
    cleared anyway (the part is physically fitted) and the shortfall is written to
    stock_log, so the gap stays visible in the audit trail instead of vanishing."""
    sale = await db.sales.find_one({"_id": obj_id(sale_id)})
    if not sale:
        raise HTTPException(status_code=404, detail="Sale not found")
    pending = sale.get("pending_fittings") or []
    if not pending:
        raise HTTPException(status_code=400, detail="No fittings pending on this sale")
    wanted  = set(body.part_numbers) if body.part_numbers else None
    inv_no  = sale.get("invoice_number", "")
    clearing = [p for p in pending if wanted is None or p.get("part_number") in wanted]
    if not clearing:
        raise HTTPException(status_code=404, detail="Those fittings are not pending on this sale")

    fittings = list(sale.get("fittings") or [])
    touched: dict = {}
    for p in clearing:
        short = int(p.get("qty_short", 0) or 0)
        pn    = p.get("part_number", "")
        part  = await db.spare_parts.find_one({"part_number": pn}) if pn else None
        take  = min(int((part or {}).get("stock", 0) or 0), short)
        if part and take > 0:
            await db.spare_parts.update_one(
                {"_id": part["_id"]},
                {"$inc": {"stock": -take}, "$push": {"stock_log": {
                    "qty": -take, "action": "subtract", "reason": "sale_fitting_late",
                    "bill_number": inv_no, "date": utcnow().isoformat(),
                }}},
            )
            touched[part["_id"]] = take
            for f in fittings:
                if f.get("part_number") == pn:
                    f["qty_deducted"] = int(f.get("qty_deducted", 0) or 0) + take
                    break
        if take < short and part:
            # Fitted from stock the system never knew about — record the discrepancy.
            await db.spare_parts.update_one(
                {"_id": part["_id"]},
                {"$push": {"stock_log": {
                    "qty": 0, "action": "note", "reason": "sale_fitting_unrecorded",
                    "shortfall": short - take, "bill_number": inv_no,
                    "date": utcnow().isoformat(),
                }}},
            )

    remaining = [p for p in pending if p not in clearing]
    await db.sales.update_one(
        {"_id": obj_id(sale_id)},
        {"$set": {"pending_fittings": remaining, "fittings": fittings,
                  "fittings_fitted_at": utcnow().isoformat(),
                  "fittings_fitted_by": current_user.get("name", "")}},
    )
    payload = oid(await db.sales.find_one({"_id": obj_id(sale_id)}))
    payload["low_stock_alerts"] = await _priority_low_alerts(touched)
    return payload


@api_router.patch("/sales/{sale_id}/milestone")
async def update_sale_milestone(sale_id: str, body: MilestoneUpdate, current_user=Depends(verify_token)):
    """Toggle a single sale milestone (documents/invoice/insurance/tax_paid/number_plate).
    When value=True, records the completion date (defaults to today if not supplied).
    When value=False, removes any previously recorded date."""
    if body.key not in SALE_MILESTONE_KEYS:
        raise HTTPException(status_code=400, detail=f"Invalid milestone key. Allowed: {SALE_MILESTONE_KEYS}")
    sale = await db.sales.find_one(
        {"_id": obj_id(sale_id)},
        {"_id": 1, "milestones": 1, "milestone_dates": 1},
    )
    if not sale:
        raise HTTPException(status_code=404, detail="Sale not found")
    # Backfill missing milestones dict if this is a legacy sale
    current       = {**default_milestones(), **(sale.get("milestones") or {})}
    current_dates = dict(sale.get("milestone_dates") or {})
    current[body.key] = bool(body.value)
    if body.value:
        d = (body.date or ist_now().strftime("%Y-%m-%d")).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
        current_dates[body.key] = d
    else:
        current_dates.pop(body.key, None)
    await db.sales.update_one(
        {"_id": obj_id(sale_id)},
        {"$set": {"milestones": current, "milestone_dates": current_dates}},
    )
    return {"id": sale_id, "milestones": current, "milestone_dates": current_dates}


@api_router.delete("/sales/{sale_id}")
async def delete_sale(sale_id: str, current_user=Depends(require_admin)):
    sale = await db.sales.find_one({"_id": obj_id(sale_id)})
    if not sale:
        raise HTTPException(status_code=404, detail="Sale not found")
    # Fittings go back on the shelf with the vehicle, or deleting a sale would quietly
    # lose the accessory stock it consumed.
    async with await _db.client.start_session() as session:
        async def _txn(s):
            await _restore_fittings(sale.get("fittings", []), sale.get("invoice_number", ""),
                                    session=s, reason="sale_fitting_delete")
            await db.vehicles.update_one({"_id": obj_id(sale["vehicle_id"])}, {"$set": {"status":"in_stock"}, "$unset": {"sold_date":"","invoice_number":""}}, session=s)
            await db.sales.delete_one({"_id": obj_id(sale_id)}, session=s)
            if sale.get("booking_id"):   # the customer still has their advance with us
                await db.vehicle_bookings.update_one(
                    {"_id": obj_id(sale["booking_id"])},
                    {"$set": {"status": "arrived"}, "$unset": {"invoice_number": "", "sale_id": "", "delivered_at": ""}},
                    session=s,
                )
        await session.with_transaction(_txn)
    await _sync_counter("invoice", "sales", "invoice_number")
    await _sheet_sync(sale, action="delete")   # drops the row; the sale is already gone
    return {"message": "Deleted"}
