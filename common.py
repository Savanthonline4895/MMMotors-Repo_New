"""Shared config and helpers used by several route modules (split out of server.py)."""
import asyncio
import json
import urllib.request
from datetime import datetime
from typing import Dict
import os
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from database import utcnow

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

# ── Register fonts once at module level ──────────────────────────────────────
_FONTS_REGISTERED = False
def _register_fonts():
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    try:
        FONT_DIR = "/usr/share/fonts/truetype/liberation/"
        DJVU_DIR = "/usr/share/fonts/truetype/dejavu/"
        pdfmetrics.registerFont(TTFont('Sans',       FONT_DIR + 'LiberationSans-Regular.ttf'))
        pdfmetrics.registerFont(TTFont('Sans-Bold',  FONT_DIR + 'LiberationSans-Bold.ttf'))
        pdfmetrics.registerFont(TTFont('Sans-Italic',FONT_DIR + 'LiberationSans-Italic.ttf'))
        pdfmetrics.registerFont(TTFont('Mono',       DJVU_DIR + 'DejaVuSansMono.ttf'))
        pdfmetrics.registerFont(TTFont('Mono-Bold',  DJVU_DIR + 'DejaVuSansMono-Bold.ttf'))
        _FONTS_REGISTERED = True
    except Exception as e:
        print(f"[MM Motors] Font registration warning: {e}")

# Change history — records who changed or deleted a record, and what changed.
# One middleware instead of a call in every handler, so a new edit endpoint is covered automatically.
_AUDIT_COLLECTIONS = {
    "sales": "sales", "service-bills": "service_bills", "parts-bills": "parts_bills",
    "parts": "spare_parts", "purchase-bills": "purchase_bills", "users": "users",
    "customers": "customers", "vehicles": "vehicles", "debts": "debts", "expenses": "expenses",
    "vehicle-bookings": "vehicle_bookings",
}

# ── Customers ─────────────────────────────────────────────────────────────────
_GSTIN_RE = r"^([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z])?$"   # 15-char GSTIN, or blank


# ─── Sale Milestones ───────────────────────────────────────────────────────
SALE_MILESTONE_KEYS = ["documents", "invoice", "insurance", "tax_paid", "number_plate"]

def default_milestones() -> Dict[str, bool]:
    return {k: False for k in SALE_MILESTONE_KEYS}


# ─── Google Sheet sync ─────────────────────────────────────────────────────────
# Mirrors each sale into the shop's existing sales spreadsheet via an Apps Script web
# app (google-sheet-sync.gs). Unset env vars = feature off, so nothing changes for an
# install that has not set it up. Setup: DEPLOY.md → "Step 8 — Google Sheet sync".
SHEETS_WEBHOOK_URL    = os.getenv("SHEETS_WEBHOOK_URL", "").strip()
SHEETS_WEBHOOK_SECRET = os.getenv("SHEETS_WEBHOOK_SECRET", "").strip()
SHEETS_TIMEOUT_S      = 8

def _sheet_date(value: str) -> str:
    """App dates come in two formats ('2026-11-03' from the form, '03 Nov 2026' from
    older records). The sheet uses '3-Nov-2026'. Unparseable input is passed through
    rather than dropped, so a stray value stays visible instead of vanishing."""
    raw = (value or "").strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d-%b-%Y"):
        try:
            d = datetime.strptime(raw, fmt)
            return f"{d.day}-{d.strftime('%b')}-{d.year}"
        except ValueError:
            continue
    return raw

def _sheet_cells(sale: dict) -> dict:
    """Sale → {column letter: value} for the shop's sheet layout.

    The single source of truth for the mapping; the Apps Script just writes what it is
    told. Columns deliberately left out so the owner keeps filling them by hand:
      A  brand/salesperson   (inconsistent format in the existing rows)
      O  RTO Done            (a status; the app's `rto` is the office code, e.g. KA07)
      R  Page Number         (the physical ledger page)
      W–AD insurance amounts, EMI, tenure, dispose, LAN, UTR (unused in the sheet)
    """
    ms    = sale.get("milestones") or {}
    dates = sale.get("milestone_dates") or {}
    nominee = sale.get("nominee") or {}
    finance_type = (sale.get("finance_type") or "").strip().lower()
    finance = "Cash" if finance_type in ("", "cash") else (sale.get("financier") or finance_type).strip()
    return {
        "B":  _sheet_date(sale.get("sale_date", "")),
        "C":  sale.get("customer_name", "") or "",
        "D":  sale.get("care_of", "") or "",
        "E":  sale.get("customer_mobile", "") or "",
        "F":  sale.get("customer_address", "") or "",
        "G":  (sale.get("vehicle_brand", "") or "").upper(),
        "H":  sale.get("vehicle_model", "") or "",
        "I":  sale.get("vehicle_color", "") or "",
        "J":  sale.get("vehicle_number", "") or "",
        "K":  sale.get("chassis_number", "") or "",
        "L":  sale.get("engine_number", "") or "",
        "M":  sale.get("hsrp_front", "") or "",
        "N":  sale.get("hsrp_back", "") or "",
        "P":  "Issued" if ms.get("number_plate") else "Not Issued",
        "Q":  _sheet_date(dates.get("number_plate", "")),
        "S":  finance,
        "T":  nominee.get("name", "") or "",
        "U":  nominee.get("relation", "") or "",
        "V":  str(nominee.get("age", "") or ""),
    }

def _sheet_post(payload: dict) -> dict:
    """Blocking POST to the Apps Script web app. urllib, not a new dependency, for a
    single call. Apps Script answers a 302 to a googleusercontent URL, which urllib
    follows by itself."""
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        SHEETS_WEBHOOK_URL, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=SHEETS_TIMEOUT_S) as resp:
        body = resp.read(4096).decode("utf-8", "replace")
    try:
        reply = json.loads(body)
    except ValueError:
        # Apps Script serves an HTML error page when a deployment is misconfigured.
        return {"ok": False, "error": f"unexpected reply: {body[:200]}"}
    # doPost always names what it did. A reply without that is not doPost answering: the
    # /exec URL redirects to a script.googleusercontent.com/macros/echo address, and that
    # address replays the doGet health JSON, whose ok:true would otherwise be recorded as
    # a row that was never written.
    if not isinstance(reply, dict):
        return {"ok": False, "error": f"unexpected reply: {body[:200]}"}
    if reply.get("ok") and not reply.get("action"):
        return {"ok": False, "error": "not the sync endpoint — SHEETS_WEBHOOK_URL must be "
                                      f"the script.google.com/macros/s/.../exec address: {body[:120]}"}
    return reply

async def _sheet_sync(sale: dict, action: str = "upsert") -> dict:
    """Mirror one sale into the sheet and stamp the outcome on the sale.

    Called only AFTER the sale is committed: the app is the record of truth and the
    sheet is a copy, so this never raises and never blocks a sale from saving. It is
    awaited rather than backgrounded so the UI can show the result straight away; the
    cost is up to SHEETS_TIMEOUT_S added to the save when Google is slow.

    A failure is recorded on the sale, never swallowed — a silent `except: pass` here is
    exactly what let the old stock bugs rot unnoticed."""
    if not SHEETS_WEBHOOK_URL or not SHEETS_WEBHOOK_SECRET:
        return {"ok": False, "skipped": "not configured"}
    payload = {
        "secret":  SHEETS_WEBHOOK_SECRET,
        "action":  action,
        "chassis": (sale.get("chassis_number") or "").strip(),
    }
    if action != "delete":
        payload["cells"] = _sheet_cells(sale)
    try:
        result = await asyncio.to_thread(_sheet_post, payload)
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    stamp = ({"sheet_synced_at": utcnow().isoformat(), "sheet_sync_error": ""}
             if result.get("ok") else
             {"sheet_synced_at": "", "sheet_sync_error": str(result.get("error", "failed"))[:300]})
    if action != "delete" and sale.get("_id") is not None:
        await db.sales.update_one({"_id": sale["_id"]}, {"$set": stamp})
    if not result.get("ok"):
        print(f"[MM Motors] sheet sync {action} failed for {sale.get('invoice_number','?')}: {stamp['sheet_sync_error']}")
    return result
