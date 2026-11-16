"""API routes: imports (split out of server.py; code moved verbatim)."""
import asyncio
import secrets
import traceback
from datetime import datetime
from fastapi import Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse
import io
from database import pwd_ctx, verify_token, require_admin, next_sequence, _sync_counter, obj_id, utcnow, ist_now, amount_in_words, canon_vehicle_status, canon_vehicle_type
import io
import csv as csv_module
from openpyxl import load_workbook
from openpyxl import Workbook
from fastapi import File, Form, UploadFile
from fastapi.responses import StreamingResponse
from routers import import_router

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

EXPENSE_CATEGORIES = [
    "Staff Salaries", "Rent & Utilities", "Vehicle Purchase",
    "Parts & Consumables", "RTO & Insurance", "Transport & Logistics",
    "Marketing & Advertising", "Bank Charges & Loan EMI",
    "Equipment & Maintenance", "Miscellaneous",
]

def safe(val, default=""):
    if val is None or str(val).strip() in ("","None","nan"):
        return default
    v = str(val).strip()
    try:
        f = float(v)
        if f == int(f):
            return str(int(f))
    except (ValueError, TypeError):
        pass
    return v

def safe_float(val, default=0.0) -> float:
    try:
        return float(str(val).replace(",","").strip())
    except (ValueError, TypeError):
        return default

def safe_int(val, default=0) -> int:
    try:
        return int(float(str(val).replace(",","").strip()))
    except (ValueError, TypeError):
        return default

def read_file(content: bytes, filename: str) -> list:
    name = (filename or "").lower()
    rows = []
    if name.endswith(".csv"):
        text   = content.decode("utf-8-sig", errors="replace")
        reader = csv_module.DictReader(io.StringIO(text))
        for row in reader:
            rows.append({k.strip().lower().replace(" ","_").replace("/","_"): safe(v) for k,v in row.items()})
    else:
        wb   = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        # Find the data sheet — prefer "Data" sheet, skip "Instructions"
        ws = None
        for sheet_name in wb.sheetnames:
            sn = sheet_name.lower()
            if "data" in sn or "📥" in sn:
                ws = wb[sheet_name]; break
        # Fallback: pick the last sheet (Instructions is first in our templates)
        if ws is None:
            ws = wb[wb.sheetnames[-1]] if len(wb.sheetnames) > 1 else wb.active
        data = list(ws.values)
        wb.close()
        if len(data) < 2:
            return []
        # Find header row (first row with actual column names, skip banner rows)
        header_row_idx = 0
        for idx, row in enumerate(data):
            non_empty = [c for c in row if c is not None and str(c).strip()]
            if len(non_empty) >= 2:
                header_row_idx = idx
                break
        headers_raw = data[header_row_idx]
        headers = [
            str(h).strip().lower()
              .replace(" ","_").replace("/","_")
              .replace("*","").replace("📥","").strip()
            if h else f"col{i}"
            for i,h in enumerate(headers_raw)
        ]
        for row in data[header_row_idx + 1:]:
            if all(v is None or str(v).strip() == "" for v in row):
                continue
            row_dict = {}
            for i in range(min(len(headers), len(row))):
                cell_val = row[i]
                # Handle Excel datetime objects
                if hasattr(cell_val, 'strftime'):
                    cell_val = cell_val.strftime("%d/%m/%Y")
                row_dict[headers[i]] = safe(cell_val)
            rows.append(row_dict)
    return rows

def result_summary(inserted, skipped, errors):
    return {
        "inserted":      inserted,
        "skipped_count": len(skipped),
        "error_count":   len(errors),
        "skipped":       skipped[:100],
        "errors":        errors[:100],
        "summary":       f"✅ {inserted} imported, ⏭ {len(skipped)} skipped, ❌ {len(errors)} errors",
    }

TEMPLATES = {
    "customers": {
        "cols":["name","mobile","care_of","email","address","tags"],
        "rows":[
            ["Ravi Kumar","9876543210","Srinivas","ravi@example.com","12 MG Road, Bengaluru","VIP"],
            ["Meena Shetty","9845123456","","","45 Koramangala, Bengaluru","Loyal"],
            ["ABC Infra Ltd","9900112233","","accounts@abc.com","HSR Layout, Bengaluru","Corporate"],
        ]
    },
    "vehicles": {
        "cols":["brand","model","variant","color","chassis_number","engine_number","vehicle_number","key_number","type","status","inbound_date","inbound_location","return_date","returned_location"],
        "rows":[
            ["HONDA","Activa 6G","STD","Pearl Black","ME4JF502RH7000001","JF50E7000001","KA01HH1234","K001","new","in_stock","01/04/2026","Showroom","",""],
            ["HERO","Splendor+","Self Start","Heavy Grey","MBLHA10EVHM000002","HA10EAHM00002","KA03AB5678","K002","new","in_stock","05/04/2026","Showroom","",""],
            ["BAJAJ","Pulsar 150","Drum","Black Red","MD2DHDZZXRCB12345","DHZDRCB12345","","","used","returned","10/03/2026","Showroom","01/04/2026","Showroom"],
        ]
    },
    "sales": {
        "cols":["customer_name","customer_mobile","care_of","vehicle_brand","vehicle_model","chassis_number",
                "engine_number","vehicle_number","vehicle_color","vehicle_variant","sale_price",
                "rto","financier","payment_mode","nominee_name","nominee_relation","nominee_age",
                "sale_date","customer_address"],
        "rows":[
            ["Ravi Kumar","9876543210","Srinivas","HONDA","Activa 6G","ME4JF502RH7000001","JF50E7000001",
             "KA01HH1234","Pearl Black","STD","80500","KA07","","Cash",
             "Balakrishna","Father","54","08/04/2026","12 MG Road, Bengaluru"],
            ["Priya Nair","9845001122","","HERO","Splendor+","MBLHA10EVHM000002","HA10EAHM00002",
             "","Heavy Grey","Self Start","73200","","HDFC Bank","Finance",
             "Suresh Nair","Husband","42","15/04/2026",""],
        ]
    },
    "service": {
        "cols":["customer_name","customer_mobile","vehicle_number","brand","model",
                "odometer_km","complaint","technician","check_in_date","status","amount","notes"],
        "rows":[
            ["Ravi Kumar","9876543210","KA01HH1234","HONDA","Activa 6G",
             "8420","Engine noise, oil change","Suresh","07/04/2026","delivered","350",""],
            ["Meena Shetty","9845123456","KA03AB5678","HERO","Splendor+",
             "12500","Routine service, brake pad","Arun","10/04/2026","delivered","500","Chain also tightened"],
        ]
    },
    "parts": {
        "cols":["part_number","name","category","brand","compatible_with","stock",
                "reorder_level","purchase_price","selling_price","gst_rate","hsn_code","location"],
        "rows":[
            ["30050-KWB-901","Spark Plug (Iridium)","Engine","NGK","HONDA,TVS","24","10","180","280","18","8511","A1-R2"],
            ["15400-PLM-A01","Oil Filter","Filters","Honda","HONDA","18","15","120","195","18","8421","A2-R1"],
            ["06435-KZR-305","Brake Pad Set (Front)","Brakes","Honda","HONDA","12","8","250","390","18","8708","B1-R3"],
        ]
    },
    "staff": {
        "cols":["name","mobile","email","username","role","salary","join_date"],
        "rows":[
            ["Rajesh Kumar","9845001122","rajesh@mmmotors.com","rajesh_k","sales","18000","01/03/2023"],
            ["Arun Shetty","9566001122","arun@mmmotors.com","arun_s","service_advisor","20000","01/04/2023"],
            ["Suresh B","9900334455","","suresh_b","technician","16000","15/06/2023"],
        ]
    },
    "expenses": {
        "cols":["date","category","sub_category","amount","description","vendor","payment_mode","receipt_no","notes"],
        "rows":[
            ["01/04/2026","Staff Salaries","","72000","April salaries — 4 staff","","Cash","",""],
            ["01/04/2026","Rent & Utilities","Rent","25000","Showroom monthly rent","Landlord","Bank","R001",""],
            ["05/04/2026","Parts & Consumables","","8500","Engine oil, grease, consumables","Spare parts supplier","Cash","",""],
            ["10/04/2026","Transport & Logistics","","3200","Vehicle delivery charges","Transport vendor","UPI","",""],
            ["15/04/2026","Marketing & Advertising","","5000","Social media ads","","UPI","",""],
        ]
    },
}

@import_router.get("/template/{entity}")
async def download_template(entity: str, current_user=Depends(verify_token)):
    if entity not in TEMPLATES:
        raise HTTPException(status_code=404, detail=f"No template for '{entity}'")
    t  = TEMPLATES[entity]
    wb = Workbook(); ws = wb.active; ws.title = entity
    ws.append(t["cols"])
    for row in t["rows"]:
        ws.append(row + [""]*(len(t["cols"])-len(row)))
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition":f'attachment; filename="template_{entity}.xlsx"'})

@import_router.post("/preview/{entity}")
async def preview_import(entity: str, file: UploadFile = File(...), current_user=Depends(verify_token)):
    if entity not in TEMPLATES:
        raise HTTPException(status_code=404, detail=f"Unknown entity: {entity}")
    content = await file.read()
    rows    = read_file(content, file.filename or "")
    return {"entity":entity,"total_rows":len(rows),"columns_found":list(rows[0].keys()) if rows else [],"preview":rows[:10],"template_cols":TEMPLATES[entity]["cols"]}

@import_router.post("/customers")
async def import_customers(request: Request, file: UploadFile = File(...), mode: str = Form("skip"), current_user=Depends(verify_token)):
    content = await file.read(); rows = read_file(content, file.filename or "")
    if not rows: raise HTTPException(status_code=400, detail="File is empty or could not be parsed")
    inserted, skipped, errors = 0, [], []

    # Dedup by mobile (primary) — same mobile = same customer
    existing_mobiles = set()
    async for doc in db.customers.find({}, {"mobile": 1}):
        if doc.get("mobile"): existing_mobiles.add(str(doc["mobile"]))

    to_insert = []
    to_update = []
    for i, row in enumerate(rows):
        if i % 200 == 0: await asyncio.sleep(0)
        rn = i + 2
        try:
            name   = safe(row.get("name"))
            mobile = safe(row.get("mobile"))
            if not name:   skipped.append({"row": rn, "reason": "Missing name"});   continue
            if not mobile: skipped.append({"row": rn, "reason": "Missing mobile"}); continue
            doc = {
                "name":       name,
                "mobile":     mobile,
                "care_of":    safe(row.get("care_of", "")),
                "email":      safe(row.get("email")),
                "address":    safe(row.get("address")),
                "tags":       [t.strip() for t in safe(row.get("tags","")).split(",") if t.strip()],
                "created_at": utcnow().isoformat(),
            }
            if mobile in existing_mobiles:
                if mode == "overwrite":
                    to_update.append(doc)
                else:
                    skipped.append({"row": rn, "reason": f"Mobile {mobile} already exists"})
            else:
                to_insert.append(doc)
                existing_mobiles.add(mobile)  # prevent intra-file dupes
        except Exception as e:
            errors.append({"row": rn, "error": str(e)})

    # Bulk insert
    if to_insert:
        try:
            result = await db.customers.insert_many(to_insert, ordered=False)
            inserted += len(result.inserted_ids)
        except Exception as e:
            errors.append({"row": "bulk", "error": str(e)})

    # Updates (sequential — need to match by mobile)
    for doc in to_update:
        try:
            await db.customers.update_one(
                {"mobile": doc["mobile"]},
                {"$set": {k: v for k, v in doc.items() if k != "created_at"}}
            )
            inserted += 1
        except Exception as e:
            errors.append({"row": "update", "error": str(e)})

    return result_summary(inserted, skipped, errors)

@import_router.post("/vehicles")
async def import_vehicles(request: Request, file: UploadFile = File(...), mode: str = Form("skip"), current_user=Depends(verify_token)):
    content = await file.read(); rows = read_file(content, file.filename or "")
    inserted, skipped, errors = 0, [], []
    existing_chassis = set()
    existing_veh_numbers = set()
    async for doc in db.vehicles.find({}, {"chassis_number":1,"vehicle_number":1}):
        if doc.get("chassis_number"): existing_chassis.add(str(doc["chassis_number"]))
        if doc.get("vehicle_number"): existing_veh_numbers.add(str(doc["vehicle_number"]).upper())
    to_insert, to_update = [], []
    for i, row in enumerate(rows):
        if i % 200 == 0: await asyncio.sleep(0)
        rn = i + 2
        try:
            chassis = safe(row.get("chassis_number","")).upper().replace(" ","")
            brand   = safe(row.get("brand","")).upper()
            model   = safe(row.get("model",""))
            veh_no  = safe(row.get("vehicle_number","")).upper()
            if not chassis: skipped.append({"row":rn,"reason":"Missing chassis_number"}); continue
            if not brand:   skipped.append({"row":rn,"reason":"Missing brand"});          continue
            if not model:   skipped.append({"row":rn,"reason":"Missing model"});          continue
            doc = {"brand":brand,"model":model,"variant":safe(row.get("variant")),"color":safe(row.get("color")),"chassis_number":chassis,"engine_number":safe(row.get("engine_number")),"vehicle_number":safe(row.get("vehicle_number")),"key_number":safe(row.get("key_number")),"type":canon_vehicle_type(safe(row.get("type","new"))),"status":canon_vehicle_status(safe(row.get("status","in_stock"))),"inbound_date":safe(row.get("inbound_date","")),"inbound_location":safe(row.get("inbound_location","")),"return_date":safe(row.get("return_date","")),"returned_location":safe(row.get("returned_location","")),"created_at":utcnow().isoformat()}
            if chassis in existing_chassis:
                if mode=="overwrite": to_update.append(doc)
                else: skipped.append({"row":rn,"reason":f"Chassis {chassis} already imported"})
            elif veh_no and veh_no in existing_veh_numbers:
                skipped.append({"row":rn,"reason":f"Vehicle number {veh_no} already imported"})
            else:
                to_insert.append(doc)
                existing_chassis.add(chassis)
                if veh_no: existing_veh_numbers.add(veh_no)
        except Exception as e:
            errors.append({"row":rn,"error":str(e)})
    if to_insert:
        try:
            r = await db.vehicles.insert_many(to_insert, ordered=False); inserted += len(r.inserted_ids)
        except Exception as e: errors.append({"row":"bulk","error":str(e)})
    for doc in to_update:
        try:
            await db.vehicles.update_one({"chassis_number":doc["chassis_number"]},{"$set":{k:v for k,v in doc.items() if k!="created_at"}}); inserted += 1
        except Exception as e: errors.append({"row":"update","error":str(e)})
    return result_summary(inserted, skipped, errors)

@import_router.post("/sales")
async def import_sales(request: Request, file: UploadFile = File(...), mode: str = Form("skip"), current_user=Depends(verify_token)):
    content = await file.read(); rows = read_file(content, file.filename or "")
    inserted, skipped, errors = 0, [], []

    # Pre-load existing dedup keys
    existing_chassis = set()   # primary dedup: chassis_number
    existing_sale_keys = set() # fallback dedup: mobile|model|sale_date (for chassis-less records)
    chassis_to_sale = {}       # chassis -> {_id, address} for address patching
    fallback_to_sale = {}      # fallback_key -> {_id, address} for address patching
    async for doc in db.sales.find({}, {"chassis_number":1,"customer_mobile":1,"vehicle_model":1,"sale_date":1,"customer_address":1}):
        ch  = str(doc.get("chassis_number","")).strip()
        mob = str(doc.get("customer_mobile","")).strip()
        mdl = str(doc.get("vehicle_model","")).strip().lower()
        dt  = str(doc.get("sale_date","")).strip()
        addr = str(doc.get("customer_address","")).strip()
        sale_ref = {"id": str(doc["_id"]), "address": addr}
        if ch:
            existing_chassis.add(ch)
            chassis_to_sale[ch] = sale_ref
        if mob and mdl and dt:
            fk = f"{mob}|{mdl}|{dt}"
            existing_sale_keys.add(fk)
            fallback_to_sale[fk] = sale_ref
    existing_inv = set()
    async for doc in db.sales.find({}, {"invoice_number": 1}):
        if doc.get("invoice_number"): existing_inv.add(str(doc["invoice_number"]))
    customer_cache = {}
    async for doc in db.customers.find({}, {"_id":1,"mobile":1}):
        if doc.get("mobile"): customer_cache[str(doc["mobile"])] = str(doc["_id"])

    # Insert row-by-row so:
    #   1. Each row gets its own error (no "Row —" bulk mystery)
    #   2. Customer is only created if the sale insert succeeds
    for i, row in enumerate(rows):
        if i % 20 == 0: await asyncio.sleep(0)
        rn = i + 2
        try:
            name   = safe(row.get("customer_name"))
            mobile = safe(row.get("customer_mobile"))
            brand  = safe(row.get("vehicle_brand","")).upper()
            model  = safe(row.get("vehicle_model",""))
            price  = safe_float(row.get("sale_price", 0))
            chassis= safe(row.get("chassis_number","")).upper().replace(" ","")

            # Validate required fields
            if not name or not mobile or not brand or not model or not price:
                missing = [f for f,v in [("customer_name",name),("customer_mobile",mobile),("vehicle_brand",brand),("vehicle_model",model),("sale_price",price)] if not v]
                skipped.append({"row":rn,"reason":f"Missing: {', '.join(missing)}"}); continue

            # Dedup: primary by chassis, fallback by mobile+model+date
            sale_date_val = safe(row.get("sale_date","")) or ist_now().strftime("%d %b %Y")
            fallback_key  = f"{mobile}|{model.lower()}|{sale_date_val}"
            import_address = safe(row.get("customer_address",""))

            if chassis and chassis in existing_chassis:
                # Sale exists — patch address if missing in DB but present in import row
                ref = chassis_to_sale.get(chassis, {})
                if import_address and not ref.get("address"):
                    await db.sales.update_one(
                        {"_id": obj_id(ref["id"])},
                        {"$set": {"customer_address": import_address}}
                    )
                    # Also patch the customer record
                    await db.customers.update_one(
                        {"mobile": mobile, "address": {"$in": ["", None]}},
                        {"$set": {"address": import_address}}
                    )
                    skipped.append({"row":rn,"reason":f"Chassis {chassis} already imported — address updated"})
                else:
                    skipped.append({"row":rn,"reason":f"Chassis {chassis} already imported"})
                continue

            if not chassis and fallback_key in existing_sale_keys:
                # Sale exists — patch address if missing
                ref = fallback_to_sale.get(fallback_key, {})
                if import_address and not ref.get("address"):
                    await db.sales.update_one(
                        {"_id": obj_id(ref["id"])},
                        {"$set": {"customer_address": import_address}}
                    )
                    await db.customers.update_one(
                        {"mobile": mobile, "address": {"$in": ["", None]}},
                        {"$set": {"address": import_address}}
                    )
                    skipped.append({"row":rn,"reason":f"Sale for {name} already imported — address updated"})
                else:
                    skipped.append({"row":rn,"reason":f"Sale for {name} ({model}, {sale_date_val}) already imported"})
                continue

            # Resolve or create customer — only after sale validation passes
            new_customer_id = None
            if mobile in customer_cache:
                cust_id = customer_cache[mobile]
            else:
                r = await db.customers.insert_one({
                    "name":name,"mobile":mobile,
                    "care_of":safe(row.get("care_of","")),
                    "email":"","address":safe(row.get("customer_address","")),
                    "tags":[],"created_at":utcnow().isoformat()
                })
                cust_id = str(r.inserted_id)
                new_customer_id = cust_id  # track so we can delete on failure
                customer_cache[mobile] = cust_id

            discount  = safe_float(row.get("discount",0))
            insurance = safe_float(row.get("insurance",0))
            rto       = safe(row.get("rto",""))          # RTO office code e.g. KA07 — stored as text
            total     = round(price - discount + insurance, 2)
            inv_no    = await next_sequence("invoice")

            doc = {
                "invoice_number": inv_no,
                "customer_id":    cust_id,
                "customer_name":  name,
                "customer_mobile":mobile,
                "care_of":        safe(row.get("care_of","")),
                "customer_address":safe(row.get("customer_address","")),
                "vehicle_brand":  brand,
                "vehicle_model":  model,
                "chassis_number": chassis,
                "engine_number":  safe(row.get("engine_number")),
                "vehicle_number": safe(row.get("vehicle_number")),
                "vehicle_color":  safe(row.get("vehicle_color")),
                "vehicle_variant":safe(row.get("vehicle_variant")),
                "sale_price":     price,
                "discount":       discount,
                "insurance":      insurance,
                "rto":            rto,  # RTO office code e.g. KA07
                "total_amount":   total,
                "amount_in_words":amount_in_words(total),
                "financier":      safe(row.get("financier","")),
                "payment_mode":   safe(row.get("payment_mode","Cash")),
                "nominee": {
                    "name":     safe(row.get("nominee_name")),
                    "relation": safe(row.get("nominee_relation")),
                    "age":      safe(row.get("nominee_age")),
                },
                "sale_date":  safe(row.get("sale_date","")) or ist_now().strftime("%d %b %Y"),
                "status":     "delivered",
                "created_at": utcnow().isoformat(),
                "_imported":  True,
            }

            # Insert sale individually — clear error per row if it fails
            try:
                await db.sales.insert_one(doc)
                inserted += 1
                if chassis: existing_chassis.add(chassis)
                existing_inv.add(inv_no)
                existing_sale_keys.add(fallback_key)
            except Exception as sale_err:
                # Roll back the newly created customer to avoid orphans
                if new_customer_id:
                    await db.customers.delete_one({"_id": obj_id(new_customer_id)})
                    del customer_cache[mobile]
                errors.append({"row": rn, "error": str(sale_err)})

        except Exception as e:
            errors.append({"row": rn, "error": str(e)})

    return result_summary(inserted, skipped, errors)

@import_router.post("/service")
async def import_service(request: Request, file: UploadFile = File(...), mode: str = Form("skip"), current_user=Depends(verify_token)):
    content = await file.read(); rows = read_file(content, file.filename or "")
    inserted, skipped, errors = 0, [], []
    # Build dedup set: vehicle_number+check_in_date
    existing_keys = set()
    async for doc in db.service_jobs.find({}, {"vehicle_number":1,"check_in_date":1}):
        if doc.get("vehicle_number") and doc.get("check_in_date"):
            existing_keys.add(f"{doc['vehicle_number']}|{doc['check_in_date']}")
    # Build customer cache
    customer_cache = {}
    async for doc in db.customers.find({}, {"_id":1,"mobile":1}):
        if doc.get("mobile"): customer_cache[str(doc["mobile"])] = str(doc["_id"])
    to_insert = []
    for i, row in enumerate(rows):
        if i % 100 == 0: await asyncio.sleep(0)
        rn = i + 2
        try:
            name     = safe(row.get("customer_name",""))
            mobile   = safe(row.get("customer_mobile",""))
            veh_no   = safe(row.get("vehicle_number","")).upper()
            complaint= safe(row.get("complaint","")) or "Service"
            check_in = safe(row.get("check_in_date","")) or ist_now().strftime("%d %b %Y")
            amount   = safe_float(row.get("amount",0))
            dedup_key = f"{veh_no}|{check_in}"
            if veh_no and check_in and dedup_key in existing_keys:
                skipped.append({"row":rn,"reason":f"Job for {veh_no} on {check_in} exists"}); continue
            if mobile in customer_cache:
                cust_id = customer_cache[mobile]
            elif name:
                r = await db.customers.insert_one({"name":name,"mobile":mobile,"email":"","address":"","tags":[],"created_at":utcnow().isoformat()})
                cust_id = str(r.inserted_id); customer_cache[mobile] = cust_id
            else: cust_id = ""
            status = safe(row.get("status","delivered")).lower()
            if status not in ("pending","in_progress","ready","delivered"): status = "delivered"
            job_no = await next_sequence("job")
            # Parse check_in_date → ISO for created_at so service_due uses real date
            try:
                parsed_checkin = datetime.strptime(check_in, "%d/%m/%Y")
            except ValueError:
                try:
                    parsed_checkin = datetime.strptime(check_in, "%d %b %Y")
                except ValueError:
                    try:
                        parsed_checkin = datetime.strptime(check_in, "%Y-%m-%d")
                    except ValueError:
                        parsed_checkin = utcnow()
            created_iso = parsed_checkin.isoformat()
            to_insert.append({"job_number":job_no,"customer_id":cust_id,"customer_name":name or "","customer_mobile":mobile or "","vehicle_number":veh_no or "","brand":safe(row.get("brand","")).upper(),"model":safe(row.get("model","")),"odometer_km":safe_int(row.get("odometer_km",0)),"complaint":complaint,"technician":safe(row.get("technician","")),"check_in_date":check_in,"status":status,"grand_total":amount,"notes":safe(row.get("notes","")),"created_at":created_iso,"_imported":True})
            if veh_no: existing_keys.add(dedup_key)
        except Exception as e:
            errors.append({"row":rn,"error":str(e)})
    if to_insert:
        try:
            r = await db.service_jobs.insert_many(to_insert, ordered=False); inserted += len(r.inserted_ids)
        except Exception as e: errors.append({"row":"bulk","error":str(e)})
    return result_summary(inserted, skipped, errors)

@import_router.post("/parts")
async def import_parts(request: Request, file: UploadFile = File(...), mode: str = Form("skip"), current_user=Depends(verify_token)):
    content = await file.read(); rows = read_file(content, file.filename or "")
    inserted, skipped, errors = 0, [], []
    existing_parts = {}
    async for doc in db.spare_parts.find({}, {"part_number":1,"stock":1,"selling_price":1,"purchase_price":1}):
        if doc.get("part_number"): existing_parts[str(doc["part_number"])] = doc
    to_insert, to_update = [], []
    for i, row in enumerate(rows):
        if i % 200 == 0: await asyncio.sleep(0)
        rn = i + 2
        try:
            part_no = safe(row.get("part_number","")).strip()
            name    = safe(row.get("name",""))
            if not part_no: skipped.append({"row":rn,"reason":"Missing part_number"}); continue
            if not name:    skipped.append({"row":rn,"reason":"Missing name"});        continue
            compat_raw = safe(row.get("compatible_with",""))
            doc = {"part_number":part_no,"name":name,"category":safe(row.get("category","")),"brand":safe(row.get("brand","")),"compatible_with":[c.strip().upper() for c in compat_raw.split(",") if c.strip()],"stock":safe_int(row.get("stock",0)),"reorder_level":safe_int(row.get("reorder_level",5)),"purchase_price":safe_float(row.get("purchase_price",0)),"selling_price":safe_float(row.get("selling_price",0)),"gst_rate":safe_float(row.get("gst_rate",18)),"hsn_code":safe(row.get("hsn_code","")),"location":safe(row.get("location","")),"created_at":utcnow().isoformat()}
            if part_no in existing_parts:
                if mode=="overwrite": to_update.append(doc)
                else: skipped.append({"row":rn,"reason":f"Part {part_no} already exists"})
            else:
                to_insert.append(doc); existing_parts[part_no] = doc
        except Exception as e:
            errors.append({"row":rn,"error":str(e)})
    if to_insert:
        try:
            r = await db.spare_parts.insert_many(to_insert, ordered=False); inserted += len(r.inserted_ids)
        except Exception as e: errors.append({"row":"bulk","error":str(e)})
    for doc in to_update:
        try:
            await db.spare_parts.update_one({"part_number":doc["part_number"]},{"$set":{k:v for k,v in doc.items() if k!="created_at"}}); inserted += 1
        except Exception as e: errors.append({"row":"update","error":str(e)})
    return result_summary(inserted, skipped, errors)

@import_router.post("/staff")
async def import_staff(file: UploadFile = File(...), mode: str = Form("skip"), current_user=Depends(require_admin)):
    content = await file.read(); rows = read_file(content, file.filename or "")
    inserted, skipped, errors = 0, [], []
    credentials = []  # one-time passwords for new accounts, returned once to the owner
    valid_roles = {"owner","sales","service_advisor","parts_counter","technician"}
    for i, row in enumerate(rows):
        rn = i+2
        try:
            name=safe(row.get("name","")); username=safe(row.get("username","")).strip().lower(); role=safe(row.get("role","sales")).strip().lower()
            if not name or not username: skipped.append({"row":rn,"reason":"Missing name or username"}); continue
            if role not in valid_roles:  skipped.append({"row":rn,"reason":f"Invalid role: {role}"});   continue
            existing = await db.users.find_one({"username":username})
            if existing:
                if mode=="overwrite":
                    await db.users.update_one({"username":username},{"$set":{"name":name,"role":role,"salary":safe_float(row.get("salary",0))}})
                    inserted += 1
                else: skipped.append({"row":rn,"reason":f"Username {username} already exists"})
                continue
            temp_password = secrets.token_urlsafe(9)
            await db.users.insert_one({"username":username,"name":name,"mobile":safe(row.get("mobile","")),"email":safe(row.get("email","")),"role":role,"password":pwd_ctx.hash(temp_password),"salary":safe_float(row.get("salary",0)),"join_date":safe(row.get("join_date","")),"status":"active","created_at":utcnow().isoformat()})
            credentials.append({"username": username, "password": temp_password})
            inserted += 1
        except Exception as e:
            traceback.print_exc(); errors.append({"row":rn,"error":str(e)})
    return {**result_summary(inserted, skipped, errors), "credentials": credentials}

@import_router.post("/expenses")
async def import_expenses(request: Request, file: UploadFile = File(...), mode: str = Form("skip"), current_user=Depends(verify_token)):
    content = await file.read(); rows = read_file(content, file.filename or "")
    inserted, skipped, errors = 0, [], []
    to_insert = []
    valid_cats = set(EXPENSE_CATEGORIES)
    # Dedup expenses by date+category+amount+description fingerprint
    existing_expense_keys = set()
    async for doc in db.expenses.find({}, {"date":1,"category":1,"amount":1,"description":1}):
        key = f"{doc.get('date','')}|{doc.get('category','')}|{doc.get('amount',0)}|{doc.get('description','')}"
        existing_expense_keys.add(key)
    for i, row in enumerate(rows):
        if i % 200 == 0: await asyncio.sleep(0)
        rn = i + 2
        try:
            date   = safe(row.get("date", "")).strip()
            cat    = safe(row.get("category", "")).strip()
            amount = safe_float(row.get("amount", 0))
            if not date:   skipped.append({"row": rn, "reason": "Missing date"}); continue
            if not amount: skipped.append({"row": rn, "reason": "Missing amount"}); continue
            if not cat:    skipped.append({"row": rn, "reason": "Missing category"}); continue
            # Dedup check
            exp_key = f"{date}|{cat}|{amount}|{safe(row.get('description',''))}"
            if exp_key in existing_expense_keys:
                skipped.append({"row": rn, "reason": "Expense already imported (same date, category, amount, description)"}); continue
            existing_expense_keys.add(exp_key)
            # Auto-match category if close
            if cat not in valid_cats:
                match = next((c for c in valid_cats if c.lower().startswith(cat.lower()[:4])), "Miscellaneous")
                cat = match
            to_insert.append({
                "date":         date,
                "category":     cat,
                "sub_category": safe(row.get("sub_category", "")),
                "amount":       amount,
                "description":  safe(row.get("description", "")),
                "vendor":       safe(row.get("vendor", "")),
                "payment_mode": safe(row.get("payment_mode", "Cash")) or "Cash",
                "receipt_no":   safe(row.get("receipt_no", "")),
                "notes":        safe(row.get("notes", "")),
                "created_by":   current_user.get("name", "imported"),
                "created_at":   utcnow().isoformat(),
            })
        except Exception as e:
            errors.append({"row": rn, "error": str(e)})
    if to_insert:
        try:
            r = await db.expenses.insert_many(to_insert, ordered=False)
            inserted += len(r.inserted_ids)
        except Exception as e:
            errors.append({"row": "bulk", "error": str(e)})
    return result_summary(inserted, skipped, errors)

@import_router.delete("/clear/{entity}")
async def clear_entity(entity: str, current_user=Depends(require_admin)):
    entity_map = {
        "customers":  ("customers","",  ""),
        "vehicles":   ("vehicles","",   ""),
        "sales":      ("sales","invoice","invoice_number"),
        "service":    ("service_jobs","job","job_number"),
        "parts":      ("spare_parts","",""),
    }
    if entity not in entity_map:
        raise HTTPException(status_code=400, detail=f"Cannot clear '{entity}'")
    coll, counter_name, counter_field = entity_map[entity]
    result = await db[coll].delete_many({})
    if counter_name and counter_field:
        await _sync_counter(counter_name, coll, counter_field)
    return {"entity":entity,"deleted":result.deleted_count}

@import_router.get("/counts")
async def import_counts(current_user=Depends(verify_token)):
    counts = await asyncio.gather(
        db.customers.count_documents({}), db.vehicles.count_documents({}),
        db.sales.count_documents({}), db.service_jobs.count_documents({}),
        db.spare_parts.count_documents({}), db.parts_sales.count_documents({}),
        db.users.count_documents({}),
    )
    return {"customers":counts[0],"vehicles":counts[1],"sales":counts[2],"service_jobs":counts[3],"spare_parts":counts[4],"parts_sales":counts[5],"users":counts[6]}
