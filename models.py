"""Pydantic request models (split out of server.py)."""
from typing import Optional, List, Dict
from pydantic import BaseModel, Field
from common import _GSTIN_RE

# ── Auth ──────────────────────────────────────────────────────────────────────
class LoginIn(BaseModel):
    username: str
    password: str

class TokenOut(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    user:         dict

# ── Users / Staff ──────────────────────────────────────────────────────────────
class UserCreate(BaseModel):
    username:      str
    name:          str
    mobile:        str
    email:         Optional[str]       = ""
    role:          str                 = "sales"
    password:      Optional[str]       = Field(None, min_length=8)  # None = generate a one-time password
    salary:        Optional[float]     = Field(0, ge=0)
    join_date:     Optional[str]       = ""
    status:        Optional[str]       = "active"
    allowed_pages: Optional[list[str]] = None  # None = use role defaults

class UserUpdate(BaseModel):
    name:          Optional[str]       = None
    mobile:        Optional[str]       = None
    email:         Optional[str]       = None
    role:          Optional[str]       = None
    salary:        Optional[float]     = Field(None, ge=0)
    status:        Optional[str]       = None
    join_date:     Optional[str]       = None
    allowed_pages: Optional[list[str]] = None

class PasswordChange(BaseModel):
    new_password: str = Field(..., min_length=8)

class CustomerCreate(BaseModel):
    name:    str
    mobile:  str
    email:   Optional[str] = ""
    address: Optional[str] = ""
    gstin:   Optional[str] = Field("", pattern=_GSTIN_RE)
    tags:    Optional[List[str]] = []
    id_proof_file_id: Optional[str] = ""
    dob:     Optional[str] = Field("", pattern=r"^(\d{4}-\d{2}-\d{2})?$")   # birthday, for greetings

class CustomerUpdate(BaseModel):
    name:    Optional[str] = None
    mobile:  Optional[str] = None
    email:   Optional[str] = None
    address: Optional[str] = None
    gstin:   Optional[str] = Field(None, pattern=_GSTIN_RE)
    # tags are managed via the dedicated admin-only PUT /customers/{id}/tags endpoint
    id_proof_file_id: Optional[str] = None
    dob:     Optional[str] = Field(None, pattern=r"^(\d{4}-\d{2}-\d{2})?$")

class CustomerTagsUpdate(BaseModel):
    tags: List[str]

# ── Badge Types (owner-managed customer badges) ────────────────────────────────
class BadgeTypeCreate(BaseModel):
    name:       str    = Field(..., min_length=1, max_length=40)
    color:      str    = Field("#888888", min_length=4, max_length=9)   # hex like #RRGGBB
    sort_order: Optional[int] = Field(100, ge=0)

class BadgeTypeUpdate(BaseModel):
    name:       Optional[str] = Field(None, min_length=1, max_length=40)
    color:      Optional[str] = Field(None, min_length=4, max_length=9)
    sort_order: Optional[int] = Field(None, ge=0)

# ── Vehicles ──────────────────────────────────────────────────────────────────
class VehicleCreate(BaseModel):
    brand:          str
    model:          str
    variant:        Optional[str] = ""
    color:          Optional[str] = ""
    chassis_number: Optional[str] = ""
    engine_number:  Optional[str] = ""
    vehicle_number: Optional[str] = ""
    key_number:     Optional[str] = ""
    purchase_price: Optional[float] = 0
    inbound_date:      Optional[str] = ""
    location:          Optional[str] = ""
    outbound_date:     Optional[str] = ""
    outbound_location: Optional[str] = ""
    status:            Optional[str] = "in_stock"
    type:           Optional[str] = "new"
    notes:          Optional[str] = ""

class VehicleUpdate(BaseModel):
    brand:          Optional[str] = None
    model:          Optional[str] = None
    variant:        Optional[str] = None
    color:          Optional[str] = None
    chassis_number: Optional[str] = None
    engine_number:  Optional[str] = None
    vehicle_number: Optional[str] = None
    purchase_price: Optional[float] = None
    inbound_date:      Optional[str] = None
    location:          Optional[str] = None
    outbound_date:     Optional[str] = None
    outbound_location: Optional[str] = None
    key_number:     Optional[str] = None
    type:           Optional[str] = None
    status:            Optional[str] = None
    notes:          Optional[str] = None

# ── Sales ─────────────────────────────────────────────────────────────────────
class NomineeInfo(BaseModel):
    name:     Optional[str] = ""
    relation: Optional[str] = ""
    age:      Optional[str] = ""
    number:   Optional[str] = ""

class SaleFitting(BaseModel):
    """An accessory fitted to the bike at sale time. Stock comes off spare_parts."""
    part_id:       Optional[str]   = None
    part_number:   Optional[str]   = ""
    name:          str
    qty:           int   = Field(1, ge=1)
    unit_price:    float = Field(0, ge=0)
    complimentary: Optional[bool]  = False

class SaleCreate(BaseModel):
    customer_id:       str
    vehicle_id:        str
    vehicle_number:    Optional[str]   = ""
    vehicle_brand:     Optional[str]   = None   # override DB value if provided
    vehicle_model:     Optional[str]   = None
    vehicle_variant:   Optional[str]   = None
    vehicle_color:     Optional[str]   = None
    chassis_number:    Optional[str]   = None
    engine_number:     Optional[str]   = None
    sale_price:        Optional[float] = 0
    total_amount:      Optional[float] = None
    finance_type:      Optional[str]   = "cash"
    financier:         Optional[str]   = ""
    loan_amount:       Optional[float] = 0
    nominee:           Optional[NomineeInfo] = None
    payment_mode:      Optional[str]   = "Cash"
    sold_by:           Optional[str]   = ""
    sale_date:         Optional[str]   = ""
    notes:             Optional[str]   = ""
    care_of:           Optional[str]   = ""
    rto:               Optional[str]   = ""   # RTO office code, e.g. KA07 — assigned after registration
    hsrp_front:        Optional[str]   = ""
    hsrp_back:         Optional[str]   = ""
    hsrp_front_id:     Optional[str]   = ""
    hsrp_back_id:      Optional[str]   = ""
    hsrp_date:         Optional[str]   = ""
    hsrp_notes:        Optional[str]   = ""
    milestones:        Optional[Dict[str, bool]] = None
    milestone_dates:   Optional[Dict[str, str]]  = None
    fittings:          Optional[List[SaleFitting]] = None

class SaleUpdate(BaseModel):
    status:            Optional[str]   = None
    delivery_date:     Optional[str]   = None
    vehicle_number:    Optional[str]   = None
    vehicle_brand:     Optional[str]   = None
    vehicle_model:     Optional[str]   = None
    vehicle_variant:   Optional[str]   = None
    vehicle_color:     Optional[str]   = None
    chassis_number:    Optional[str]   = None
    engine_number:     Optional[str]   = None
    payment_mode:      Optional[str]   = None
    notes:             Optional[str]   = None
    customer_name:     Optional[str]   = None
    customer_mobile:   Optional[str]   = None
    customer_address:  Optional[str]   = None
    vehicle_id:        Optional[str]   = None
    total_amount:      Optional[float] = None
    sale_price:        Optional[float] = None
    finance_type:      Optional[str]   = None
    financier:         Optional[str]   = None
    loan_amount:       Optional[float] = None
    nominee:           Optional[NomineeInfo] = None
    sale_date:         Optional[str]   = None
    sold_by:           Optional[str]   = None
    care_of:           Optional[str]   = None
    rto:               Optional[str]   = None
    hsrp_front:        Optional[str]   = None
    hsrp_back:         Optional[str]   = None
    hsrp_front_id:     Optional[str]   = None
    hsrp_back_id:      Optional[str]   = None
    hsrp_date:         Optional[str]   = None
    hsrp_notes:        Optional[str]   = None
    milestones:        Optional[Dict[str, bool]] = None
    milestone_dates:   Optional[Dict[str, str]]  = None
    fittings:          Optional[List[SaleFitting]] = None


class MilestoneUpdate(BaseModel):
    key:   str
    value: bool
    date:  Optional[str] = None   # YYYY-MM-DD; required-ish when value=True (defaults to today)

# ── Service Jobs ──────────────────────────────────────────────────────────────
class ServiceJobCreate(BaseModel):
    customer_id:    str
    vehicle_number: str
    brand:          str
    model:          str
    variant:        Optional[str] = ""
    chassis_number: Optional[str] = ""
    odometer_km:    Optional[int] = 0
    complaint:      str
    advisor_id:     Optional[str] = ""
    technician:     Optional[str] = ""
    check_in_date:  Optional[str] = ""
    estimated_delivery: Optional[str] = ""
    notes:          Optional[str] = ""
    vehicle_photo_id: Optional[str] = ""
    save_vehicle:   Optional[bool] = False
    service_type:   Optional[str] = ""   # "Repair / Walk-in" or "1st Service" … "6th+ Service"

class FeedbackSubmit(BaseModel):
    rating:  int           = Field(..., ge=1, le=5)
    comment: Optional[str] = Field("", max_length=1000)

class ServiceJobUpdate(BaseModel):
    status:             Optional[str] = None
    technician:         Optional[str] = None
    estimated_delivery: Optional[str] = None
    delivery_date:      Optional[str] = None
    notes:              Optional[str] = None
    complaint:          Optional[str] = None
    vehicle_number:     Optional[str] = None
    chassis_number:     Optional[str] = None
    brand:              Optional[str] = None
    model:              Optional[str] = None
    odometer_km:        Optional[int] = None
    odometer_out:       Optional[int] = None
    vehicle_photo_id:   Optional[str] = None
    check_in_date:      Optional[str] = None
    created_at:         Optional[str] = None   # correcting imported service dates
    customer_name:      Optional[str] = None
    customer_mobile:    Optional[str] = None
    service_type:       Optional[str] = None

# ── Service Bills ─────────────────────────────────────────────────────────────
class BillLineItem(BaseModel):
    description:   str
    part_number:   Optional[str] = ""
    hsn_code:      Optional[str] = ""
    qty:           int           = Field(1, ge=1)
    unit_price:    float         = Field(..., ge=0)
    gst_rate:      float         = Field(18, ge=0, le=100)
    discount:      Optional[float] = Field(0, ge=0)
    complimentary: Optional[bool] = False

class ServiceBillCreate(BaseModel):
    job_id:          str
    labour_charges:  Optional[float] = Field(0, ge=0)
    labour_gst_rate: Optional[float] = Field(18, ge=0, le=100)
    labour_discount: Optional[float] = Field(0, ge=0)
    items:           Optional[List[BillLineItem]] = []
    payment_mode:    Optional[str] = "Cash"
    notes:           Optional[str] = ""
    discount:        Optional[float] = Field(0, ge=0)

class ServiceBillUpdate(BaseModel):
    items:           Optional[List[BillLineItem]] = None
    payment_mode:    Optional[str]   = None
    notes:           Optional[str]   = None
    discount:        Optional[float] = Field(None, ge=0)

# ── Spare Parts ───────────────────────────────────────────────────────────────
class SparePartCreate(BaseModel):
    part_number:     Optional[str] = ""
    name:            str
    category:        Optional[str] = ""
    brand:           Optional[str] = ""
    compatible_with: Optional[List[str]] = []
    stock:           int    = Field(0, ge=0)
    reorder_level:   int    = Field(5, ge=0)
    purchase_price:  float  = Field(..., ge=0)
    selling_price:   float  = Field(..., ge=0)
    gst_rate:        float  = Field(18, ge=0, le=100)
    hsn_code:        Optional[str] = ""
    location:        Optional[str] = ""
    priority:        bool   = False   # fast-moving: only these raise low-stock alerts
    fitting_brands:  Optional[List[str]] = []   # standard fitting for these vehicle brands
    fitting_qty:     int    = Field(1, ge=1)    # how many go on one bike

class SparePartUpdate(BaseModel):
    name:            Optional[str] = None
    category:        Optional[str] = None
    brand:           Optional[str] = None
    compatible_with: Optional[List[str]] = None
    stock:           Optional[int]   = Field(None, ge=0)
    reorder_level:   Optional[int]   = Field(None, ge=0)
    purchase_price:  Optional[float] = Field(None, ge=0)
    selling_price:   Optional[float] = Field(None, ge=0)
    gst_rate:        Optional[float] = Field(None, ge=0, le=100)
    hsn_code:        Optional[str] = None
    location:        Optional[str] = None
    priority:        Optional[bool] = None
    fitting_brands:  Optional[List[str]] = None
    fitting_qty:     Optional[int] = Field(None, ge=1)

# PATCH 4: Added action field ─────────────────────────────────────────────────
class StockAdjust(BaseModel):
    qty:    int    # positive = stock in, negative = adjustment
    action: Optional[str] = "add"   # "add" | "subtract"
    reason: Optional[str] = ""

class PartOrderedUpdate(BaseModel):
    ordered: bool = True   # False clears the flag (ordered by mistake / cancelled)

class FittingsFittedUpdate(BaseModel):
    """Clears a sale's pending-fitting marker once the parts are physically on the bike."""
    part_numbers: Optional[List[str]] = None   # None = every pending fitting on the sale


# ── Vendors ──────────────────────────────────────────────────────────────────
class VendorCreate(BaseModel):
    name:         str
    gstin:        Optional[str] = ""
    address:      Optional[str] = ""
    phone:        Optional[str] = ""
    email:        Optional[str] = ""
    bank_name:    Optional[str] = ""
    bank_account: Optional[str] = ""
    bank_ifsc:    Optional[str] = ""
    notes:        Optional[str] = ""

class VendorUpdate(BaseModel):
    name:         Optional[str] = None
    gstin:        Optional[str] = None
    address:      Optional[str] = None
    phone:        Optional[str] = None
    email:        Optional[str] = None
    bank_name:    Optional[str] = None
    bank_account: Optional[str] = None
    bank_ifsc:    Optional[str] = None
    notes:        Optional[str] = None


# ── Purchase Bills (from vendors) ────────────────────────────────────────────
class PurchaseBillItemIn(BaseModel):
    part_id:        Optional[str] = None   # null = create new spare_part
    part_name:      str
    part_number:    Optional[str] = ""     # vendor's SKU
    hsn:            Optional[str] = ""
    qty:            float           = Field(..., gt=0)
    unit:           Optional[str] = "PCS"
    rate:           float           = Field(..., ge=0)   # per-unit rate before discount
    discount_pct:   Optional[float] = Field(0, ge=0, le=100)
    gst_pct:        Optional[float] = Field(18, ge=0, le=100)
    # Optional per-line sale price update, and new-part hints
    new_sale_price: Optional[float] = Field(None, ge=0)
    new_part_selling_price: Optional[float] = Field(None, ge=0)  # only used if creating new part

class PurchaseBillCreate(BaseModel):
    vendor_id:        str
    bill_number:      str
    bill_date:        str                  # DD Mon YYYY preferred
    place_of_supply:  Optional[str] = ""
    items:            List[PurchaseBillItemIn]
    round_off:        Optional[float] = 0
    notes:            Optional[str] = ""

class PurchaseBillUpdate(BaseModel):
    bill_number:      Optional[str] = None
    bill_date:        Optional[str] = None
    place_of_supply:  Optional[str] = None
    items:            Optional[List[PurchaseBillItemIn]] = None
    round_off:        Optional[float] = None
    notes:            Optional[str] = None

# PATCH 3: Parts Bills models ─────────────────────────────────────────────────
class PartsBillItem(BaseModel):
    part_id:         Optional[str]   = None
    part_number:     Optional[str]   = ""
    name:            str
    hsn_code:        Optional[str]   = "8714"
    qty:             int             = Field(1, ge=1)
    unit_price:      float           = Field(..., ge=0)
    gst_rate:        float           = Field(18.0, ge=0, le=100)
    discount:        Optional[float] = Field(0, ge=0)
    complimentary:   Optional[bool]  = False

class PartsBillCreate(BaseModel):
    customer_name:    Optional[str] = ""
    customer_mobile:  Optional[str] = ""
    customer_vehicle: Optional[str] = ""
    payment_mode:     Optional[str] = "Cash"
    items:            List[PartsBillItem] = []
    discount:         Optional[float] = Field(0, ge=0)

class PartsBillUpdate(BaseModel):
    customer_name:    Optional[str] = None
    customer_mobile:  Optional[str] = None
    customer_vehicle: Optional[str] = None
    payment_mode:     Optional[str] = None
    items:            Optional[List[PartsBillItem]] = None
    discount:         Optional[float] = Field(None, ge=0)


# ─── Accident Estimates ───────────────────────────────────────────────────────
class AccidentEstimatePartItem(BaseModel):
    part_name:  str   = ""
    part_number: str  = ""
    condition:  str   = "New OEM"
    qty:        float = Field(1, ge=0)
    unit_price: float = Field(0, ge=0)
    gst:        float = Field(18, ge=0, le=100)

class AccidentEstimateLabourItem(BaseModel):
    description: str   = ""
    hours:       float = Field(1, ge=0)
    rate:        float = Field(0, ge=0)

class AccidentEstimateCreate(BaseModel):
    vehicle:     dict = {}
    customer:    dict = {}
    incident:    dict = {}
    parts:       List[AccidentEstimatePartItem]   = []
    labour:      List[AccidentEstimateLabourItem] = []
    additional:  dict = {}
    notes:       str  = ""
    grand_total: float = 0
    status:      str  = "draft"

class AccidentEstimateUpdate(BaseModel):
    vehicle:     Optional[dict]  = None
    customer:    Optional[dict]  = None
    incident:    Optional[dict]  = None
    parts:       Optional[List[AccidentEstimatePartItem]]   = None
    labour:      Optional[List[AccidentEstimateLabourItem]] = None
    additional:  Optional[dict]  = None
    notes:       Optional[str]   = None
    grand_total: Optional[float] = None
    status:      Optional[str]   = None
