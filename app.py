import argparse
import os, json, time, uuid, re
from datetime import datetime, date, timedelta, timezone
from io import BytesIO
from flask import Flask, render_template, request, redirect, url_for, flash, session, abort, send_from_directory, send_file
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
import msal
from urllib.parse import urlencode
from werkzeug.utils import secure_filename
from openpyxl import Workbook

app = Flask(__name__)

# --- Jinja filter: split (for environments that lack it) ---


@app.get("/healthz")
def healthz():
    try:
        # quick DB check
        db.session.execute(db.text("SELECT 1"))
        return {"ok": True, "db": "up"}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500




from flask import request, jsonify

@app.route("/sms/send", methods=["POST"])
def sms_send():
    """Send an SMS via MyMobileAPI BulkMessages.
    Expected JSON: {"destination": "+27...", "message": "text", "testMode": false}
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        dest = (data.get("destination") or "").strip()
        msg  = (data.get("message") or "").strip()
        # optional testMode flag
        test_mode = bool(data.get("testMode", False))

        if not dest or not msg:
            return jsonify({"ok": False, "error": "Missing destination or message"}), 400

        # Build Basic auth header
        import base64, requests
        auth_raw = f"{MYMOBILEAPI_USERNAME}:{MYMOBILEAPI_PASSWORD}".encode("utf-8")
        auth_hdr = "Basic " + base64.b64encode(auth_raw).decode("ascii")

        payload = {
            "sendOptions": {"testMode": test_mode},
            "messages": [
                {"destination": dest, "content": msg}
            ]
        }

        headers = {
            "Authorization": auth_hdr,
            "accept": "application/json",
            "content-type": "application/json",
        }

        # Post to MyMobileAPI
        try:
            resp = requests.post(MYMOBILEAPI_URL, json=payload, headers=headers, timeout=15)
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text}

            if resp.status_code == 200:
                return jsonify({"ok": True, "status": resp.status_code, "response": body})
            elif resp.status_code in (400,401,500,503):
                return jsonify({"ok": False, "status": resp.status_code, "response": body}), resp.status_code
            else:
                return jsonify({"ok": False, "status": resp.status_code, "response": body}), resp.status_code
        except requests.RequestException as e:
            return jsonify({"ok": False, "error": str(e)}), 502

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.template_filter("split")
def jinja_split(value, sep="|"):
    if value is None:
        return []
    return str(value).split(sep)

# =========================
# Provider rename map (canonicalization)
# =========================
RENAME_MAP = {
    "Umvuzo Fedhealth": "Intelligene Fedhealth",
    "Umvuzo Intelligene": "Intelligene Umvuzo",  # legacy
}
REVERSE_MAP = {}
for _old, _new in RENAME_MAP.items():
    REVERSE_MAP.setdefault(_new, set()).add(_old)

def normalize_provider(name: str | None) -> str | None:
    if not name:
        return name
    return RENAME_MAP.get(name, name)

# --- Demo timer sample (not used by main orders view) ---
orders_demo = [
    {"id": 1, "name": "Medicine A", "ordered_at": "2025-08-30 10:00", "assigned": 1, "required": 2, "created_at": datetime.now(timezone.utc)},
    {"id": 2, "name": "Medicine B", "ordered_at": "2025-08-29 08:00", "assigned": 0, "required": 1, "created_at": datetime.now(timezone.utc) - timedelta(hours=25)},
    {"id": 3, "name": "Medicine C", "ordered_at": "2025-08-30 15:00", "assigned": 2, "required": 2, "created_at": datetime.now(timezone.utc) - timedelta(hours=5)},
]


def time_left(obj):
    """Return remaining time info using UTC-aware datetimes.
    Accepts either a dict (expects 'created_at' and optional 'sla_hours')
    or an object with attribute 'created_at'.
    """
    from datetime import datetime, timedelta, timezone

    def ensure_utc(dt):
        if dt is None:
            return None
        if isinstance(dt, str):
            try:
                # fromisoformat may produce naive dt; attach UTC
                dtp = datetime.fromisoformat(dt)
                if dtp.tzinfo is None:
                    dtp = dtp.replace(tzinfo=timezone.utc)
                else:
                    dtp = dtp.astimezone(timezone.utc)
                return dtp
            except Exception:
                return None
        # dt is datetime
        if getattr(dt, "tzinfo", None) is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    if isinstance(obj, dict):
        created = ensure_utc(obj.get("created_at"))
        sla_hours = obj.get("sla_hours", 24)
    else:
        created = ensure_utc(getattr(obj, "created_at", None))
        sla_hours = 24

    if not created:
        return {"remaining_hours": None, "overdue": False}

    expiry_time = created + timedelta(hours=sla_hours)
    now_utc = datetime.now(timezone.utc)
    remaining = expiry_time - now_utc
    remaining_hours = round(remaining.total_seconds() / 3600.0, 1)
    return {"remaining_hours": remaining_hours, "overdue": remaining_hours < 0}


app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-key-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///life360.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Uploads
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_ROOT = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_ROOT, exist_ok=True)
ALLOWED_EXT = {"pdf","doc","docx","xls","xlsx","csv","png","jpg","jpeg","txt","ppt","pptx"}

# Azure (optional)
TENANT_ID = os.environ.get("AZURE_TENANT_ID", "common")
CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "")
AUTHORITY = os.environ.get("AZURE_AUTHORITY", f"https://login.microsoftonline.com/{TENANT_ID}")
REDIRECT_PATH = os.environ.get("AZURE_REDIRECT_PATH", "/getAToken")
SCOPE = os.environ.get("AZURE_SCOPE", "User.Read")

db = SQLAlchemy(app)

# Canonical provider list (in use)
PROVIDERS = [
    "Geneway", "Optiway", "Enbiosis", "Intelligene", "Healthy Me",
    "Intelligene Fedhealth", "Geko",
]

# ---------------- Models ----------------
class StockItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    expiry_date = db.Column(db.Date, nullable=True)
    received_date = db.Column(db.Date, nullable=True)
    # These remain for backward compatibility but are not shown in UI:
    code_type = db.Column(db.String(10), nullable=False, default="Kit")
    person_requested = db.Column(db.String(120), nullable=True)
    request_datetime = db.Column(db.DateTime, nullable=True)
    current_stock = db.Column(db.Integer, nullable=False, default=0)
    provider = db.Column(db.String(120), nullable=True)

class StockUnit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    barcode = db.Column(db.String(120), unique=True, nullable=False)
    batch_number = db.Column(db.String(120), nullable=True)  # <-- NEW: per-barcode batch no.
    status = db.Column(db.String(40), nullable=False, default="In Stock")
    item_id = db.Column(db.Integer, db.ForeignKey('stock_item.id'), nullable=False)
    item = db.relationship("StockItem", backref=db.backref("units", lazy="dynamic"))
    last_update = db.Column(db.DateTime, default=datetime.utcnow)

class OrderUnit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, nullable=False)  # link to in-memory order id
    unit_id = db.Column(db.Integer, db.ForeignKey('stock_unit.id'), nullable=False)
    unit = db.relationship("StockUnit")
    assigned_at = db.Column(db.DateTime, default=datetime.utcnow)



class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(120), nullable=True)
    name = db.Column(db.String(120), nullable=True)
    surname = db.Column(db.String(120), nullable=True)
    practitioner_name = db.Column(db.String(120), nullable=True)
    ordered_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(40), nullable=False, default="Pending")
    notes = db.Column(db.Text, nullable=True)
    email_status = db.Column(db.String(60), nullable=True)
    sent_out = db.Column(db.Boolean, default=False)
    received_back = db.Column(db.Boolean, default=False)
    kit_registered = db.Column(db.Boolean, default=False)
    results_sent = db.Column(db.Boolean, default=False)
    paid = db.Column(db.Boolean, default=False)
    invoiced = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

    items = db.relationship("OrderItem", backref="order", cascade="all, delete-orphan", lazy=True)

class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    sku = db.Column(db.String(120), nullable=False)
    qty = db.Column(db.Integer, nullable=False, default=1)
class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    provider = db.Column(db.String(120), nullable=True)
    assignee = db.Column(db.String(120), nullable=True)
    due_date = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(40), nullable=False, default="Open")  # Open, In Progress, Done
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(120), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)  # unique on disk
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

class OrderCallLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, nullable=False)
    when = db.Column(db.DateTime, default=datetime.utcnow)
    author = db.Column(db.String(120), nullable=True)
    summary = db.Column(db.Text, nullable=False)
    outcome = db.Column(db.String(60), nullable=True)

# ---------------- Dummy Data ----------------
PRACTITIONERS = []
ORDERS = []

# ---------- Helpers ----------
def listify_interests(v):
    if not v:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).replace("\r", "\n")
    for ch in ["•", ";", "|", ","]:
        s = s.replace(ch, "\n")
    parts = [p.strip(" -\t") for p in s.split("\n")]
    return [p for p in parts if p]

def parse_date(v):
    if not v: return None
    try:
        return datetime.fromisoformat(v).date() if "T" in v else date.fromisoformat(v)
    except: return None

def parse_dt(v):
    if not v: return None
    try:
        return datetime.fromisoformat(v)
    except: return None

def batch_summary_for_item(item_id: int) -> str:
    """Return a compact Batch # summary for an item: single code or 'Mixed (N)'."""
    vals = [r[0] for r in db.session.query(StockUnit.batch_number)
            .filter(StockUnit.item_id == item_id).distinct().all()]
    vals = [v for v in vals if v]  # drop None/empty
    if not vals:
        return "-"
    if len(vals) == 1:
        return vals[0]
    return f"Mixed ({len(vals)})"

def seed_demo_if_empty():
    """Populate demo data; normalize providers to canonical names."""
    global PRACTITIONERS, ORDERS
    if not PRACTITIONERS:
        PRACTITIONERS = [
            {"id": 1, "provider": "Geneway", "title": "Ms", "first_name": "Thandi", "last_name": "Mkhize",
             "name": "Thandi", "surname": "Mkhize", "signed_up": "2025-08-20",
             "email": "thandi@example.com", "phone": "+27821234567",
             "occupation": "Dietitian", "city": "Cape Town", "province": "Western Cape", "postal_code": "8001",
             "registered_with_board": True,
             "interests": ["Genetic Screening", "Nutrigenomics"],
             "notes": "Cape Town clinic.",
             "onboarded": True, "training": True, "website": True, "whatsapp": True, "engagebay": True},
            {"id": 2, "provider": "Optiway", "title": "Mr", "first_name": "Sipho", "last_name": "Dlamini",
             "name": "Sipho", "surname": "Dlamini", "signed_up": "2025-08-22",
             "email": "sipho@example.com", "phone": "+27842223344",
             "occupation": "Health Coach", "city": "Pretoria", "province": "Gauteng", "postal_code": "0181",
             "registered_with_board": False,
             "interests": "Preventative Health, Microbiome",
             "notes": "Focus on microbiome kits.",
             "onboarded": False, "training": False, "website": False, "whatsapp": False, "engagebay": False},
            {"id": 3, "provider": "Enbiosis", "title": "Ms", "first_name": "Lerato", "last_name": "Moabi",
             "name": "Lerato", "surname": "Moabi", "signed_up": "2025-08-12",
             "email": "lerato@example.com", "phone": "+27839911111",
             "occupation": "Dietician", "city": "Bloemfontein", "province": "Free State", "postal_code": "9301",
             "registered_with_board": True,
             "interests": ["Gut Health", "Diet Planning"],
             "notes": "Dietician partner.",
             "onboarded": True, "training": True, "website": True, "whatsapp": True, "engagebay": False},
            {"id": 4, "provider": "Intelligene", "title": "Dr", "first_name": "Aisha", "last_name": "Patel",
             "name": "Aisha", "surname": "Patel", "signed_up": "2025-07-29",
             "email": "aisha.patel@example.com", "phone": "+27825559090",
             "occupation": "Functional Medicine Practitioner", "city": "Johannesburg", "province": "Gauteng", "postal_code": "2191",
             "registered_with_board": True,
             "interests": "Autoimmunity; Nutrigenomics; Lifestyle Medicine",
             "notes": "New Intelligene contact.",
             "onboarded": False, "training": True, "website": True, "whatsapp": True, "engagebay": False},
            {"id": 5, "provider": "Healthy Me", "title": "Mrs", "first_name": "Naledi", "last_name": "Khoza",
             "name": "Naledi", "surname": "Khoza", "signed_up": "2025-08-25",
             "email": "naledi@example.com", "phone": "+27831112222",
             "occupation": "Nutritionist", "city": "Randburg", "province": "Gauteng", "postal_code": "2194",
             "registered_with_board": False,
             "interests": ["Weight Loss", "Women’s Health"],
             "notes": "Johannesburg North.",
             "onboarded": True, "training": True, "website": True, "whatsapp": True, "engagebay": True},
            {"id": 6, "provider": "Intelligene Fedhealth", "title": "Mr", "first_name": "Kea", "last_name": "Molefe",
             "name": "Kea", "surname": "Molefe", "signed_up": "2025-08-18",
             "email": "kea@example.com", "phone": "+27823334444",
             "occupation": "Health Coach", "city": "Midrand", "province": "Gauteng", "postal_code": "1685",
             "registered_with_board": False,
             "interests": "Chronic Disease Prevention | Fitness",
             "notes": "Fedhealth channel.",
             "onboarded": True, "training": True, "website": True, "whatsapp": True, "engagebay": True},
            {"id": 7, "provider": "Geko", "title": "Ms", "first_name": "Zanele", "last_name": "Nkosi",
             "name": "Zanele", "surname": "Nkosi", "signed_up": "2025-08-23",
             "email": "zanele.nkosi@example.com", "phone": "+27835551212",
             "occupation": "Wellness Practitioner", "city": "Durban", "province": "KwaZulu-Natal", "postal_code": "4001",
             "registered_with_board": False,
             "interests": ["Sleep", "Stress Management", "Metabolic Health"],
             "notes": "",
             "onboarded": False, "training": False, "website": False, "whatsapp": True, "engagebay": False},
            {"id": 8, "provider": "Geneway", "title": "Dr", "first_name": "Ridwaan", "last_name": "Cassim",
             "name": "Ridwaan", "surname": "Cassim", "signed_up": "2025-08-26",
             "email": "rcassim@example.com", "phone": "+27827770001",
             "occupation": "General Practitioner", "city": "Port Elizabeth", "province": "Eastern Cape", "postal_code": "6001",
             "registered_with_board": True,
             "interests": "Cardiometabolic; Preventative Medicine",
             "notes": "New to Geneway program.",
             "onboarded": False, "training": True, "website": False, "whatsapp": False, "engagebay": False},
        ]
        for p in PRACTITIONERS:
            p["provider"] = normalize_provider(p.get("provider"))

def migrate_orders_to_db():
    global ORDERS
    """One-time migration: copy in-memory demo ORDERS into DB if DB has no orders yet."""
    try:
        if db.session.query(Order).count() == 0 and ORDERS:
            for o in ORDERS:
                order = Order(
                    id=o.get("id"),
                    provider=normalize_provider(o.get("provider")),
                    name=o.get("name"),
                    surname=o.get("surname"),
                    practitioner_name=o.get("practitioner_name"),
                    ordered_at=datetime.fromisoformat(o.get("ordered_at")) if o.get("ordered_at") else datetime.now(timezone.utc),
                    status=o.get("status") or "Pending",
                    notes=o.get("notes"),
                    email_status=o.get("email_status"),
                    sent_out=bool(o.get("sent_out")),
                    received_back=bool(o.get("received_back")),
                    kit_registered=bool(o.get("kit_registered")),
                    results_sent=bool(o.get("results_sent")),
                    paid=bool(o.get("paid")),
                    invoiced=bool(o.get("invoiced")),
                    created_at=o.get("created_at") or datetime.now(timezone.utc),
                    completed_at=o.get("completed_at") if isinstance(o.get("completed_at"), datetime) else None,
                )
                db.session.add(order)
                for it in (o.get("items") or []):
                    db.session.add(OrderItem(order=order, sku=it.get("sku") or "SKU", qty=int(it.get("qty") or 1)))
            db.session.commit()
    except Exception as e:
        print("migrate_orders_to_db error:", e)


    if not ORDERS:
        ORDERS = [
            {"id": 101, "provider": "Geneway", "name": "Thandi", "surname": "Mkhize", "ordered_at": "2025-08-27T09:30:00",
             "items": [{"sku":"KIT-GEN-01","qty":3}], "status":"Pending", "notes":"Demo order.",
             "email_status":"ok", "sent_out": False, "received_back": False, "kit_registered": False,
             "results_sent": False, "paid": False, "invoiced": False, "practitioner_name": ""},
            {"id": 102, "provider": "Optiway", "name": "Sipho", "surname": "Dlamini", "ordered_at": "2025-08-26T14:10:00",
             "items": [{"sku":"OPT-START","qty":2},{"sku":"OPT-PRO","qty":1}], "status":"Pending", "notes":"Urgent.",
             "email_status":"ok", "sent_out": False, "received_back": False, "kit_registered": False,
             "results_sent": False, "paid": False, "invoiced": False, "practitioner_name": ""},
            {"id": 103, "provider": "Enbiosis", "name": "Lerato", "surname": "Moabi", "ordered_at": "2025-08-20T10:00:00",
             "items": [{"sku":"ENB-BIO","qty":5}], "status":"Completed", "notes":"Completed batch.",
             "email_status":"ok", "sent_out": True, "received_back": True, "kit_registered": True,
             "results_sent": True, "paid": True, "invoiced": True, "practitioner_name": "Lerato Moabi"},
            {"id": 104, "provider": "Intelligene", "name": "Aisha", "surname": "Patel", "ordered_at": "2025-08-24T16:45:00",
             "items": [{"sku":"INT-GEN","qty":4}], "status":"Pending", "notes":"Follow up.",
             "email_status":"ok", "sent_out": False, "received_back": False, "kit_registered": False,
             "results_sent": False, "paid": False, "invoiced": False, "practitioner_name": ""},
            {"id": 105, "provider": "Healthy Me", "name": "Naledi", "surname": "Khoza", "ordered_at": "2025-08-26T12:00:00",
             "items": [{"sku":"HM-START","qty":3}], "status":"Pending", "notes":"",
             "email_status":"ok", "sent_out": False, "received_back": False, "kit_registered": False,
             "results_sent": False, "paid": False, "invoiced": False, "practitioner_name": ""},
            {"id": 106, "provider": "Intelligene Fedhealth", "name": "Kea", "surname": "Molefe", "ordered_at": "2025-08-22T11:20:00",
             "items": [{"sku":"FED-BUNDLE","qty":2}], "status":"Completed", "notes":"",
             "email_status":"ok", "sent_out": True, "received_back": True, "kit_registered": True,
             "results_sent": True, "paid": True, "invoiced": True, "practitioner_name": "Kea Molefe"},
        ]
        for o in ORDERS:
            o["provider"] = normalize_provider(o.get("provider"))

def bucket_order(o):
    return 'completed' if all([o.get('received_back'), o.get('kit_registered'), o.get('results_sent'), o.get('paid'), o.get('invoiced')]) or o.get('status') == 'Completed' else 'pending'

# ---------------- Dashboard ----------------

@app.route("/")
def dashboard():
    user = session.get("user")
    seed_demo_if_empty()
    migrate_orders_to_db()
    total_prac = len(PRACTITIONERS)
    onboarded = sum(1 for p in PRACTITIONERS if p["onboarded"])
    pending_prac = total_prac - onboarded

    # Orders from DB
    total_orders = db.session.query(Order).count()
    completed_orders = db.session.query(Order).filter(Order.status.ilike("%completed%")).count()
    cancelled_orders = db.session.query(Order).filter(Order.status.ilike("%cancel%")).count()
    pending_orders = total_orders - completed_orders - cancelled_orders

    # Recent orders and chart data
    orders = []
    for o in db.session.query(Order).order_by(Order.created_at.desc()).limit(100).all():
        items = [{"sku": it.sku, "qty": it.qty} for it in o.items]
        orders.append({
            "id": o.id, "provider": o.provider, "name": o.name, "surname": o.surname,
            "status": o.status, "created_at": o.created_at.isoformat() if o.created_at else None,
            "completed_at": o.completed_at.isoformat() if o.completed_at else None,
            "items": items,
            "sent_out": o.sent_out, "received_back": o.received_back, "kit_registered": o.kit_registered,
            "results_sent": o.results_sent, "paid": o.paid, "invoiced": o.invoiced,
        })

    return render_template("dashboard.html", user=user,
                           total_prac=total_prac, onboarded=onboarded, pending_prac=pending_prac,
                           total_orders=total_orders, completed_orders=completed_orders, pending_orders=pending_orders,
                           orders=orders)
@app.route("/practitioners")
def practitioners():
    user = session.get("user")
    seed_demo_if_empty()
    for p in PRACTITIONERS:
        p["provider"] = normalize_provider(p.get("provider"))
        p["interests_list"] = listify_interests(p.get("interests"))
    buckets = {'pending': {}, 'completed': {}}
    for p in PRACTITIONERS:
        b = 'completed' if p.get('onboarded') else 'pending'
        buckets[b].setdefault(p['provider'], []).append(p)
    return render_template("practitioners.html", user=user, buckets=buckets)

@app.route("/practitioners/<int:pid>/update", methods=["POST"])
def practitioners_update(pid):
    for p in PRACTITIONERS:
        if p["id"] == pid:
            p["training"] = 'training' in request.form
            p["website"] = 'website' in request.form
            p["whatsapp"] = 'whatsapp' in request.form
            p["engagebay"] = 'engagebay' in request.form
            p["onboarded"] = 'onboarded' in request.form
            p["interests_list"] = listify_interests(p.get("interests"))
            flash("Practitioner flags updated.", "success")
            break
    return redirect(url_for("practitioners"))

# ---------------- Orders + Call Logs ----------------

@app.route("/orders", endpoint="orders")
def orders_view():
    seed_demo_if_empty()
    migrate_orders_to_db()

    # Fetch all orders from DB, newest first
    db_orders = db.session.query(Order).order_by(Order.created_at.desc()).all()
    orders = []
    for o in db_orders:
        orders.append({
            "id": o.id, "provider": o.provider, "name": o.name, "surname": o.surname,
            "ordered_at": o.ordered_at.isoformat() if o.ordered_at else None,
            "created_at": o.created_at or datetime.now(timezone.utc),
            "status": o.status, "notes": o.notes or "",
            "email_status": o.email_status or "ok",
            "sent_out": o.sent_out, "received_back": o.received_back, "kit_registered": o.kit_registered,
            "results_sent": o.results_sent, "paid": o.paid, "invoiced": o.invoiced,
            "practitioner_name": o.practitioner_name or "",
            "items": [{"sku": it.sku, "qty": it.qty} for it in o.items],
            "time_left": time_left({"created_at": o.created_at or datetime.now(timezone.utc)}),
        })
    assigned_units = {}
    for ou in OrderUnit.query.all():
        assigned_units.setdefault(ou.order_id, []).append(ou)

    return render_template("orders.html",
                           user=session.get("user"),
                           orders=orders,
                           assigned_units=assigned_units,
                           call_logs=OrderCallLog.query.all())
@app.route("/stock")
def stock():
    user = session.get("user")
    items = StockItem.query.order_by(StockItem.id.desc()).all()
    counts = {}
    by_provider = {}
    batch_summary = {}
    for i in items:
        prov = normalize_provider(i.provider) or "Unassigned"
        total = db.session.query(func.count(StockUnit.id)).filter(StockUnit.item_id == i.id).scalar()
        in_stock = db.session.query(func.count(StockUnit.id)).filter(StockUnit.item_id == i.id, StockUnit.status == "In Stock").scalar()
        counts[i.id] = {"total": total, "in_stock": in_stock}
        by_provider.setdefault(prov, []).append(i)
        batch_summary[i.id] = batch_summary_for_item(i.id)
    providers_sorted = sorted([p for p in by_provider.keys() if p != "Unassigned"]) + (["Unassigned"] if "Unassigned" in by_provider else [])
    return render_template("stock.html", user=user, counts=counts, by_provider=by_provider, providers=providers_sorted, batch_summary=batch_summary)

@app.route("/new")
def new_item():
    user = session.get("user")
    return render_template("new_item.html", user=user)

@app.post("/items")
def create_item():
    item = StockItem(
        name=request.form.get("name","").strip(),
        expiry_date=parse_date(request.form.get("expiry_date")),
        received_date=parse_date(request.form.get("received_date")),
        # the following are kept but no longer shown in UI:
        code_type="Kit",
        person_requested=None,
        request_datetime=None,
        current_stock=int(request.form.get("current_stock",0)),
        provider=normalize_provider(request.form.get("provider")) or "Unassigned",
    )
    if not item.name:
        flash("Name is required.", "error"); return redirect(url_for("new_item"))
    db.session.add(item); db.session.commit()
    flash("Stock item added.", "success")
    return redirect(url_for("stock"))

@app.route("/item/<int:item_id>/units")
def manage_units(item_id):
    user = session.get("user")
    item = StockItem.query.get_or_404(item_id)
    units = StockUnit.query.filter_by(item_id=item_id).order_by(StockUnit.id.desc()).all()
    return render_template("units.html", user=user, item=item, units=units)

@app.post("/item/<int:item_id>/units/add_one")
def add_unit_one(item_id):
    item = StockItem.query.get_or_404(item_id)
    barcode = (request.form.get("barcode") or "").strip()
    batch_number = (request.form.get("batch_number") or "").strip() or None
    if not barcode:
        flash("Scan or enter a barcode.", "error"); return redirect(url_for("manage_units", item_id=item_id))
    if StockUnit.query.filter_by(barcode=barcode).first():
        flash("This barcode already exists.", "error"); return redirect(url_for("manage_units", item_id=item_id))
    u = StockUnit(barcode=barcode, batch_number=batch_number, item_id=item_id, status="In Stock", last_update=datetime.now(timezone.utc))
    db.session.add(u); db.session.commit()
    flash(f"Added barcode {barcode}.", "success")
    return redirect(url_for("manage_units", item_id=item_id))

@app.post("/item/<int:item_id>/units/add_bulk")
def add_units_bulk(item_id):
    item = StockItem.query.get_or_404(item_id)
    raw = request.form.get("barcodes","")
    default_batch = (request.form.get("batch_number") or "").strip() or None
    new_count = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line: 
            continue
        # Accept formats: "BARCODE,BATCH", "BARCODE\tBATCH", "BARCODE | BATCH"
        parts = [p.strip() for p in re.split(r'[,\t|]+', line, maxsplit=1) if p.strip()]
        barcode = parts[0] if parts else ""
        if not barcode:
            continue
        batch_no = parts[1] if len(parts) > 1 else default_batch
        if StockUnit.query.filter_by(barcode=barcode).first():
            continue
        db.session.add(StockUnit(barcode=barcode, batch_number=batch_no, item_id=item_id, status="In Stock", last_update=datetime.now(timezone.utc)))
        new_count += 1
    db.session.commit()
    flash(f"Added {new_count} barcodes.", "success")
    return redirect(url_for("manage_units", item_id=item_id))

@app.post("/unit/<int:unit_id>/delete")
def delete_unit(unit_id):
    u = StockUnit.query.get_or_404(unit_id)
    if OrderUnit.query.filter_by(unit_id=unit_id).first():
        flash("Cannot delete: unit assigned to order.", "error")
        return redirect(url_for("manage_units", item_id=u.item_id))
    item_id = u.item_id
    db.session.delete(u); db.session.commit()
    flash("Unit deleted.", "success")
    return redirect(url_for("manage_units", item_id=item_id))

# ---------------- Tasks ----------------
@app.route("/tasks")
def tasks_home():
    user = session.get("user")
    q = Task.query.order_by(Task.status.desc(), Task.due_date.asc().nullslast(), Task.created_at.desc()).all()
    return render_template("tasks.html", user=user, tasks=q)

@app.post("/tasks/add")
def tasks_add():
    title = request.form.get("title","").strip()
    if not title:
        flash("Task needs a title.", "error"); return redirect(url_for("tasks_home"))
    t = Task(
        title=title,
        provider=normalize_provider(request.form.get("provider")) or None,
        assignee=request.form.get("assignee") or None,
        due_date=parse_date(request.form.get("due_date")),
        status=request.form.get("status") or "Open",
        notes=request.form.get("notes") or None
    )
    db.session.add(t); db.session.commit()
    flash("Task added.", "success")
    return redirect(url_for("tasks_home"))

@app.post("/tasks/<int:tid>/update")
def tasks_update(tid):
    t = Task.query.get_or_404(tid)
    t.title = request.form.get("title", t.title)
    t.provider = normalize_provider(request.form.get("provider")) or t.provider
    t.assignee = request.form.get("assignee") or t.assignee
    t.due_date = parse_date(request.form.get("due_date")) or t.due_date
    t.status = request.form.get("status") or t.status
    t.notes = request.form.get("notes") or t.notes
    db.session.commit()
    flash("Task updated.", "success")
    return redirect(url_for("tasks_home"))

@app.post("/tasks/<int:tid>/delete")
def tasks_delete(tid):
    t = Task.query.get_or_404(tid)
    db.session.delete(t); db.session.commit()
    flash("Task deleted.", "success")
    return redirect(url_for("tasks_home"))

# ---------------- Reports (Excel Export) ----------------
@app.route("/reports")
def reports():
    user = session.get("user")
    return render_template("reports.html", user=user)

def _wb_from_list_dict(rows, headers):
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append([r.get(h, "") for h in headers])
    return wb

@app.route("/export/practitioners.xlsx")
def export_practitioners():
    seed_demo_if_empty()
    rows = []
    for p in PRACTITIONERS:
        rows.append({
            "ID": p["id"],
            "Provider": p["provider"],
            "Name": f"{p.get('name') or p.get('first_name','')} {p.get('surname') or p.get('last_name','')}".strip(),
            "Email": p.get("email",""),
            "Phone": p.get("phone") or p.get("phone_e164",""),
            "SignedUp": p.get("signed_up",""),
            "Onboarded": "Yes" if p.get("onboarded") else "No",
            "Training": "Yes" if p.get("training") else "No",
            "Website": "Yes" if p.get("website") else "No",
            "WhatsApp": "Yes" if p.get("whatsapp") else "No",
            "EngageBay": "Yes" if p.get("engagebay") else "No",
            "Notes": p.get("notes",""),
        })
    headers = ["ID","Provider","Name","Email","Phone","SignedUp","Onboarded","Training","Website","WhatsApp","EngageBay","Notes"]
    wb = _wb_from_list_dict(rows, headers)
    bio = BytesIO(); wb.save(bio); bio.seek(0)
    return send_file(bio, as_attachment=True, download_name="practitioners.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.route("/export/orders.xlsx")
def export_orders():
    seed_demo_if_empty()
    rows = []
    for o in ORDERS:
        rows.append({
            "ID": o["id"],
            "Provider": o["provider"],
            "Name": f"{o['name']} {o['surname']}",
            "OrderedAt": o["ordered_at"],
            "Status": o["status"],
            "SentOut": "Yes" if o["sent_out"] else "No",
            "ReceivedBack": "Yes" if o["received_back"] else "No",
            "KitRegistered": "Yes" if o["kit_registered"] else "No",
            "ResultsSent": "Yes" if o["results_sent"] else "No",
            "Paid": "Yes" if o["paid"] else "No",
            "Invoiced": "Yes" if o["invoiced"] else "No",
            "PractitionerName": o["practitioner_name"],
            "Items": "; ".join([f"{it['sku']} x{it['qty']}" for it in o["items"]]),
            "Notes": o["notes"],
        })
    headers = ["ID","Provider","Name","OrderedAt","Status","SentOut","ReceivedBack","KitRegistered","ResultsSent","Paid","Invoiced","PractitionerName","Items","Notes"]
    wb = _wb_from_list_dict(rows, headers)
    bio = BytesIO(); wb.save(bio); bio.seek(0)
    return send_file(bio, as_attachment=True, download_name="orders.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ---------------- Uploads (by provider) ----------------
@app.route("/uploads")
def uploads_home():
    user = session.get("user")
    files_by_provider = {}
    for p in PROVIDERS:
        rows = Document.query.filter_by(provider=p).order_by(Document.uploaded_at.desc()).all()
        files_by_provider[p] = rows
    return render_template("uploads.html", user=user, providers=PROVIDERS, files_by_provider=files_by_provider)

@app.post("/uploads/add")
def upload_file():
    provider = normalize_provider(request.form.get("provider")) or "Unassigned"
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a file.", "error"); return redirect(url_for("uploads_home"))
    ext = f.filename.rsplit(".",1)[-1].lower() if "." in f.filename else ""
    if ext not in ALLOWED_EXT:
        flash("File type not allowed.", "error"); return redirect(url_for("uploads_home"))
    prov_dir = os.path.join(UPLOAD_ROOT, (provider or "Unassigned").replace(" ", "_"))
    os.makedirs(prov_dir, exist_ok=True)
    stored = secure_filename(f"{uuid.uuid4().hex}_{f.filename}")
    f.save(os.path.join(prov_dir, stored))
    doc = Document(provider=provider or "Unassigned", filename=f.filename, stored_name=stored)
    db.session.add(doc); db.session.commit()
    flash("File uploaded.", "success"); return redirect(url_for("uploads_home"))

def _provider_dir_candidates(provider: str):
    norm = normalize_provider(provider) or provider or "Unassigned"
    dirs = [os.path.join(UPLOAD_ROOT, norm.replace(" ", "_"))]
    for old in REVERSE_MAP.get(norm, []):
        dirs.append(os.path.join(UPLOAD_ROOT, old.replace(" ", "_")))
    if provider != norm:
        dirs.append(os.path.join(UPLOAD_ROOT, provider.replace(" ", "_")))
    seen = set(); out = []
    for d in dirs:
        if d not in seen:
            out.append(d); seen.add(d)
    return out

@app.route("/uploads/<provider>/<stored_name>")
def download_uploaded(provider, stored_name):
    for d in _provider_dir_candidates(provider):
        fp = os.path.join(d, stored_name)
        if os.path.exists(fp):
            return send_from_directory(d, stored_name, as_attachment=True)
    abort(404)

# ---------------- Azure demo auth (optional) ----------------
def _build_msal_app(cache=None, authority=None):
    return msal.ConfidentialClientApplication(
        CLIENT_ID, authority=authority or f"https://login.microsoftonline.com/{TENANT_ID}",
        client_credential=CLIENT_SECRET, token_cache=cache
    )

def _build_auth_url(scopes=None, state=None):
    from flask import url_for
    return _build_msal_app().get_authorization_request_url(
    scopes or ["User.Read"],
    state=state or "default",
    redirect_uri=url_for("authorized", _external=True)
    )

@app.route("/login")
def login():
    if not os.environ.get("AZURE_CLIENT_ID"):
        flash("Azure AD not configured (demo login).", "error")
        return redirect(url_for("dashboard"))
    return redirect(_build_auth_url())

@app.route(REDIRECT_PATH)
def authorized():
    session["user"] = {"name":"Demo User","preferred_username":"demo@example.com"}
    flash("Signed in (demo). Configure Azure to enable real auth.", "success")
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    params = {"post_logout_redirect_uri": url_for("dashboard", _external=True)}
    authority = os.environ.get("AZURE_AUTHORITY", f"https://login.microsoftonline.com/{TENANT_ID}")
    return redirect(f"{authority}/oauth2/v2.0/logout?{urlencode(params)}")

# ---------------- One-time DB rename utility ----------------
def apply_provider_renames_db():
    changed = False
    for model in (StockItem, Task, Document):
        for old, new in RENAME_MAP.items():
            q = db.session.query(model).filter(getattr(model, 'provider') == old)
            if q.count():
                q.update({'provider': new})
                changed = True
    if changed:
        db.session.commit()




@app.get("/orders/new")
def new_order_form():
    providers = ["Geneway","Optiway","Enbiosis","Reboot","Intelligene","Healthy Me","Intelligene Fedhealth","Geko"]
    return render_template("order_new.html", user=session.get("user"), providers=providers)

@app.post("/orders/new")
def create_order():
    provider = normalize_provider(request.form.get("provider"))
    name = (request.form.get("name") or "").strip()
    surname = (request.form.get("surname") or "").strip()
    practitioner_name = (request.form.get("practitioner_name") or "").strip()
    notes = (request.form.get("notes") or "").strip()
    ordered_at_str = (request.form.get("ordered_at") or "").strip()
    try:
        ordered_at = datetime.fromisoformat(ordered_at_str) if ordered_at_str else datetime.now(timezone.utc)
    except Exception:
        ordered_at = datetime.now(timezone.utc)
    status = request.form.get("status") or "Pending"
    o = Order(provider=provider, name=name, surname=surname, practitioner_name=practitioner_name,
              notes=notes, ordered_at=ordered_at, status=status, created_at=datetime.now(timezone.utc))
    db.session.add(o); db.session.flush()

    # Up to 3 items from form fields item_sku_1..3, item_qty_1..3
    for i in range(1, 4):
        sku = (request.form.get(f"item_sku_{i}") or "").strip()
        qty = request.form.get(f"item_qty_{i}")
        if sku and qty:
            try: q = int(qty)
            except: q = 1
            db.session.add(OrderItem(order_id=o.id, sku=sku, qty=q))
    db.session.commit()
    flash(f"Order #{o.id} created.", "success")
    return redirect(url_for("orders"))



import base64, requests

# ======== MyMobileAPI credentials (provided by user) ========
MYMOBILEAPI_USERNAME = "f029c437-4ae2-49ad-89a0-d5a20319b29f"
MYMOBILEAPI_PASSWORD = "E0qcstyjMF4K/jGPyqQ+FTqjtwCmrrhQ"
MYMOBILEAPI_URL = "https://rest.mymobileapi.com/v3/BulkMessages"
# ============================================================

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "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")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemini-2.5-flash-image-preview:free"




@app.post("/orders/<int:order_id>/update", endpoint="update_order")
def orders_update(order_id):
    o = Order.query.get(order_id)
    if not o:
        flash("Order not found.", "error")
        return redirect(url_for("orders"))
    # Basic fields
    o.practitioner_name = (request.form.get("practitioner_name") or o.practitioner_name or "").strip() or None
    o.status = (request.form.get("status") or o.status or "Pending").strip()
    o.notes = (request.form.get("notes") or o.notes or "").strip() or None

    # Workflow checkboxes
    def as_bool(name):
        v = request.form.get(name)
        return True if v in ("on","true","1","yes") else False
    o.sent_out = as_bool("sent_out")
    o.received_back = as_bool("received_back")
    o.kit_registered = as_bool("kit_registered")
    o.results_sent = as_bool("results_sent")
    o.paid = as_bool("paid")
    o.invoiced = as_bool("invoiced")

    # Completed timestamp
    if o.status.lower().startswith("completed"):
        if not o.completed_at:
            o.completed_at = datetime.now(timezone.utc)
    else:
        o.completed_at = None

    o.email_status = (request.form.get("email_status") or o.email_status or "").strip() or None

    db.session.commit()
    flash(f"Order #{o.id} updated.", "success")
    return redirect(url_for("orders") + f"#o{o.id}")


@app.post("/orders/<int:order_id>/add_calllog", endpoint="add_calllog")
def orders_add_calllog(order_id):
    o = Order.query.get(order_id)
    if not o:
        flash("Order not found.", "error")
        return redirect(url_for("orders"))
    author = (request.form.get("author") or "").strip() or None
    summary = (request.form.get("summary") or "").strip()
    outcome = (request.form.get("outcome") or "").strip() or None
    if not summary:
        flash("Call log requires a summary.", "error")
        return redirect(url_for("orders") + f"#o{order_id}")
    cl = OrderCallLog(order_id=order_id, author=author, summary=summary, outcome=outcome)
    db.session.add(cl)
    db.session.commit()
    flash("Call log added.", "success")
    return redirect(url_for("orders") + f"#o{order_id}")


@app.post("/orders/<int:order_id>/assign")
def assign_unit(order_id):
    barcode = (request.form.get("barcode") or "").strip()
    if not barcode:
        flash("Scan or enter a barcode.", "error")
        return redirect(url_for("orders") + f"#o{order_id}")

    unit = StockUnit.query.filter_by(barcode=barcode).first()
    if not unit:
        flash("Barcode not found in stock.", "error")
        return redirect(url_for("orders") + f"#o{order_id}")

    if unit.status != "In Stock":
        flash(f"Unit {barcode} is not available (status: {unit.status}).", "error")
        return redirect(url_for("orders") + f"#o{order_id}")

    db.session.add(OrderUnit(order_id=order_id, unit_id=unit.id))
    unit.status = "Assigned"
    unit.last_update = datetime.now(timezone.utc)
    db.session.commit()

    flash(f"Assigned {barcode} to order #{order_id}.", "success")
    return redirect(url_for("orders") + f"#o{order_id}")


@app.post("/orders/<int:order_id>/unassign/<int:ou_id>")
def unassign_unit(order_id, ou_id):
    ou = OrderUnit.query.get_or_404(ou_id)
    if ou.order_id != order_id:
        abort(400)
    unit = ou.unit
    db.session.delete(ou)
    unit.status = "In Stock"
    unit.last_update = datetime.now(timezone.utc)
    db.session.commit()
    flash("Unassigned barcode.", "success")
    return redirect(url_for("orders") + f"#o{order_id}")













@app.post("/api/ask_ai")
def ask_ai():
    """
    Smarter intent-aware AI endpoint.
    Answers common dashboard queries directly from DB.
    Falls back to OpenRouter only if needed.
    """
    from datetime import datetime, timedelta, timezone
    import re, json

    data = request.get_json(force=True, silent=True) or {}
    raw_prompt = (data.get("prompt") or "").strip()
    prompt = raw_prompt.lower()

    def wants_json():
        return " as json" in prompt or prompt.strip().endswith("json") or "json please" in prompt

    # --- Parsers ---
    def parse_days(text, default=30):
        m = re.search(r'(\d+)\s*(day|days|d)\b', text)
        return int(m.group(1)) if m else default

    def parse_threshold(text, default=2):
        m = re.search(r'(?:<=|under)\s*(\d+)', text)
        return int(m.group(1)) if m else default

    def parse_status(text):
        if "completed" in text: return "completed"
        if "pending" in text: return "pending"
        if "cancel" in text: return "cancelled"
        return None

    def parse_provider(text):
        providers = ["Geneway","Optiway","Enbiosis","Reboot","Intelligene","Healthy Me","Intelligene Fedhealth","Geko"]
        for p in providers:
            if p.lower() in text:
                return p
        return None

    def parse_order_id(text):
        m = re.search(r'(?:order\s*#?|id\s*#?)(\d+)', text)
        return int(m.group(1)) if m else None

    # --- Quick answers ---
    total = db.session.query(Order).count()
    completed = db.session.query(Order).filter(Order.status.ilike("%completed%")).count()
    cancelled = db.session.query(Order).filter(Order.status.ilike("%cancel%")).count()
    pending = total - completed - cancelled

    if "pending orders" in prompt and "how many" in prompt:
        return {"ok": True, "answer": f"{pending} pending orders."}
    if "completed orders" in prompt and "how many" in prompt:
        return {"ok": True, "answer": f"{completed} completed orders."}

    # low stock
    if "low" in prompt and "stock" in prompt:
        thr = parse_threshold(prompt, 2)
        low = []
        for si in db.session.query(StockItem).order_by(StockItem.current_stock.asc()).limit(100):
            if (si.current_stock or 0) <= thr:
                low.append({"name": si.name, "qty": si.current_stock, "provider": si.provider})
        if wants_json():
            return {"ok": True, "items": low, "threshold": thr, "total": len(low)}
        return {"ok": True, "answer": ", ".join([f"{x['name']}({x['qty']})" for x in low[:20]]) or "No low-stock items found."}

    # expiring stock
    if "expiring" in prompt and "stock" in prompt:
        days = parse_days(prompt, 30)
        from datetime import date
        today = date.today()
        horizon = today + timedelta(days=days)
        soon = []
        for si in db.session.query(StockItem).filter(StockItem.expiry_date != None).order_by(StockItem.expiry_date.asc()).limit(200):
            if si.expiry_date and today <= si.expiry_date <= horizon:
                soon.append({"name": si.name, "expires": si.expiry_date.isoformat(), "provider": si.provider})
        if wants_json():
            return {"ok": True, "items": soon, "days": days, "total": len(soon)}
        return {"ok": True, "answer": ", ".join([f"{x['name']}→{x['expires']}" for x in soon[:20]]) or f"No items expiring in {days} days."}

    # order by id
    oid = parse_order_id(prompt)
    if oid:
        row = db.session.query(Order).get(oid)
        if not row:
            return {"ok": True, "answer": f"Order #{oid} not found."}
        full = f"{(row.name or '').strip()} {(row.surname or '').strip()}".strip() or "Unknown"
        return {"ok": True, "answer": f"Order #{row.id}: {full} · {row.provider or '—'} · {row.status or '—'}"}

    # fallback to OpenRouter with context
    prov = {row[0] or "Unknown": row[1] for row in db.session.query(Order.provider, db.func.count(Order.id)).group_by(Order.provider)}
    low = []
    for si in db.session.query(StockItem).order_by(StockItem.current_stock.asc()).limit(20):
        if (si.current_stock or 0) <= 2:
            low.append({"name": si.name, "qty": si.current_stock})

    context = (
        f"Orders: total={total}, completed={completed}, pending={pending}, cancelled={cancelled}. "
        f"Providers: {prov}. Low stock (<=2): {low}."
    )
    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer": request.host_url.rstrip('/'),
            "X-Title": "Life360 Dashboard Ask AI"
        }
        payload = {
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role":"system","content":"Be concise, numeric. Provide short lists for stock/orders."},
                {"role":"user","content": f"{context}\n\nQuestion: {raw_prompt}"}
            ]
        }
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60)
        j = resp.json()
        text = (j.get("choices") or [{}])[0].get("message", {}).get("content", "") if isinstance(j, dict) else ""
        return {"ok": True, "answer": text or "No answer."}
    except Exception:
        return {"ok": True, "answer": f"Orders total={total}, completed={completed}, pending={pending}. Low stock: " + (", ".join([f"{x['name']}({x['qty']})" for x in low]) or "none")}


if __name__ == "__main__":
    import logging, sys, os, traceback
    from datetime import timezone, datetime
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    port = int(os.environ.get("PORT", "5000"))
    logging.info("Starting Life360 app...")
    logging.info("Python %s", sys.version.replace("\n"," "))
    logging.info("Binding on 0.0.0.0:%s", port)

    try:
        with app.app_context():
            # Ensure DB schema exists
            db.create_all()
            if 'apply_provider_renames_db' in globals():
                apply_provider_renames_db()
            if 'seed_demo_if_empty' in globals():
                seed_demo_if_empty()
            if 'migrate_orders_to_db' in globals():
                migrate_orders_to_db()
        logging.info("DB initialized successfully.")
    except Exception as e:
        logging.error("DB initialization failed: %s", e)
        traceback.print_exc()
        sys.exit(1)

    try:
        logging.info("Health check: /healthz")
        # Not actually calling HTTP, just log that it's available
        logging.info("Visit http://localhost:%s/healthz", port)
        logging.info("Open the dashboard at http://localhost:%s/", port)
        app.run(host="0.0.0.0", port=port, debug=True)
    except Exception as e:
        logging.error("Server failed to start: %s", e)
        traceback.print_exc()
        sys.exit(1)
