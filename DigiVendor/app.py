"""DigiVendor - Digital Marketplace & Financial Inclusion Platform for Street Vendors.

Flask backend + SQLite. Run locally:  python app.py
Run on Render:                        gunicorn app:app
"""
import io
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import qrcode
from qrcode.constants import ERROR_CORRECT_M
from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "digivendor.db"))
IST = timezone(timedelta(hours=5, minutes=30))

AREAS = [
    "Mira Road East", "Mira Road West", "Bhayandar East",
    "Bhayandar West", "Kashimira", "Naya Nagar",
]
CATEGORIES = [
    "Vegetables", "Fruits", "Street Food", "Snacks",
    "Cobbler", "Artisan", "Flowers", "Other",
]

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me-in-production")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_name   TEXT NOT NULL,
    owner_name  TEXT NOT NULL,
    phone       TEXT NOT NULL,
    area        TEXT NOT NULL,
    category    TEXT NOT NULL,
    upi_id      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id  INTEGER NOT NULL,
    name       TEXT NOT NULL,
    category   TEXT,
    price      REAL NOT NULL,
    FOREIGN KEY (vendor_id) REFERENCES vendors(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS sales (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id  INTEGER NOT NULL,
    amount     REAL NOT NULL,
    mode       TEXT NOT NULL CHECK (mode IN ('Cash', 'Digital')),
    sale_date  TEXT NOT NULL,
    FOREIGN KEY (vendor_id) REFERENCES vendors(id) ON DELETE CASCADE
);
"""

DEMO_VENDORS = [
    ("Ramesh Sabzi Wala", "Ramesh Yadav", "9876543210", "Mira Road East", "Vegetables", "ramesh@upi",
     [("Tomato (1 kg)", 40), ("Potato (1 kg)", 30), ("Onion (1 kg)", 35), ("Spinach (bunch)", 15)]),
    ("Sunita Fruit Stall", "Sunita Patil", "9822012345", "Bhayandar West", "Fruits", "sunita@upi",
     [("Banana (dozen)", 60), ("Apple (1 kg)", 160), ("Papaya (1 kg)", 50), ("Watermelon (1 kg)", 25)]),
    ("Gupta Chaat Corner", "Mohan Gupta", "9867001122", "Mira Road East", "Street Food", "guptachaat@upi",
     [("Pani Puri (plate)", 30), ("Sev Puri (plate)", 40), ("Bhel Puri", 35), ("Dahi Puri", 50)]),
    ("Shinde Vada Pav", "Santosh Shinde", "9920334455", "Bhayandar East", "Street Food", "shindevp@upi",
     [("Vada Pav", 20), ("Misal Pav", 60), ("Cutting Chai", 10)]),
    ("Fatima Flowers", "Fatima Shaikh", "9930112233", "Kashimira", "Flowers", "fatima@upi",
     [("Marigold Garland", 40), ("Rose (bunch)", 50), ("Jasmine Gajra", 20)]),
    ("Lakhan Cobbler", "Lakhan Chauhan", "9769445566", "Bhayandar West", "Cobbler", "lakhan@upi",
     [("Shoe Polish", 20), ("Sole Repair", 120), ("Chappal Stitching", 40)]),
]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    count = conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0]
    if count == 0:
        now = datetime.now(IST)
        for shop, owner, phone, area, cat, upi, items in DEMO_VENDORS:
            cur = conn.execute(
                "INSERT INTO vendors (shop_name, owner_name, phone, area, category, upi_id) "
                "VALUES (?, ?, ?, ?, ?, ?)", (shop, owner, phone, area, cat, upi))
            vid = cur.lastrowid
            for name, price in items:
                conn.execute(
                    "INSERT INTO items (vendor_id, name, category, price) VALUES (?, ?, ?, ?)",
                    (vid, name, cat, price))
            # a few demo sales so the dashboard is not empty
            demo_sales = [(250, "Digital", 0), (120, "Cash", 0), (480, "Digital", 1), (90, "Cash", 1)]
            for amount, mode, days_ago in demo_sales:
                when = (now - timedelta(days=days_ago, hours=1)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT INTO sales (vendor_id, amount, mode, sale_date) VALUES (?, ?, ?, ?)",
                    (vid, amount, mode, when))
        conn.commit()
    conn.close()


init_db()  # runs on import, so it also works under gunicorn


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def now_ist():
    return datetime.now(IST)


def wa_number(phone):
    """Return a WhatsApp-ready number (country code + 10 digits)."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 10:
        digits = "91" + digits
    return digits


app.jinja_env.filters["wa_number"] = wa_number


def get_stats(vendor_id):
    db = get_db()
    today = now_ist().strftime("%Y-%m-%d")
    row = db.execute(
        """SELECT
             COALESCE(SUM(CASE WHEN date(sale_date) = ? THEN amount END), 0) AS today_sales,
             COALESCE(SUM(amount), 0) AS total_sales,
             COALESCE(SUM(CASE WHEN mode = 'Digital' THEN amount END), 0) AS digital_total
           FROM sales WHERE vendor_id = ?""", (today, vendor_id)).fetchone()
    total = row["total_sales"]
    share = round(row["digital_total"] / total * 100, 1) if total else 0.0
    return {
        "today_sales": round(row["today_sales"], 2),
        "total_sales": round(total, 2),
        "digital_share": share,
    }


def current_vendor():
    vid = session.get("vendor_id")
    if not vid:
        return None
    return get_db().execute("SELECT * FROM vendors WHERE id = ?", (vid,)).fetchone()


def api_vendor_or_401():
    vendor = current_vendor()
    if vendor is None:
        abort(401)
    return vendor


def parse_positive_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0 or number > 10_000_000 or number != number:
        return None
    return round(number, 2)


@app.errorhandler(401)
def unauthorized(_e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Please log in as a vendor first."}), 401
    return redirect(url_for("vendor"))


# --------------------------------------------------------------------------
# Customer view
# --------------------------------------------------------------------------
@app.route("/")
def index():
    area = request.args.get("area", "").strip()
    category = request.args.get("category", "").strip()
    db = get_db()

    query = "SELECT * FROM vendors WHERE 1=1"
    params = []
    if area:
        query += " AND area = ?"
        params.append(area)
    if category:
        query += " AND category = ?"
        params.append(category)
    query += " ORDER BY shop_name COLLATE NOCASE"

    vendors = []
    for v in db.execute(query, params).fetchall():
        items = db.execute(
            "SELECT id, name, price FROM items WHERE vendor_id = ? ORDER BY name COLLATE NOCASE",
            (v["id"],)).fetchall()
        vendors.append({"info": v, "items": items})

    return render_template("index.html", vendors=vendors, areas=AREAS,
                           categories=CATEGORIES, sel_area=area, sel_category=category)


# --------------------------------------------------------------------------
# Vendor login / register
# --------------------------------------------------------------------------
@app.route("/vendor")
def vendor():
    vendors = get_db().execute(
        "SELECT id, shop_name, owner_name FROM vendors ORDER BY shop_name COLLATE NOCASE").fetchall()
    return render_template("vendor.html", vendors=vendors, areas=AREAS, categories=CATEGORIES)


@app.route("/vendor/login", methods=["POST"])
def vendor_login():
    vid = request.form.get("vendor_id", type=int)
    row = get_db().execute("SELECT id FROM vendors WHERE id = ?", (vid,)).fetchone() if vid else None
    if row is None:
        flash("Please select a valid shop.", "error")
        return redirect(url_for("vendor"))
    session["vendor_id"] = row["id"]
    return redirect(url_for("dashboard"))


@app.route("/vendor/register", methods=["POST"])
def vendor_register():
    f = request.form
    shop_name = f.get("shop_name", "").strip()[:80]
    owner_name = f.get("owner_name", "").strip()[:80]
    phone = re.sub(r"\D", "", f.get("phone", ""))
    area = f.get("area", "").strip()
    category = f.get("category", "").strip()
    upi_id = f.get("upi_id", "").strip()[:80]

    error = None
    if not shop_name or not owner_name:
        error = "Shop name and owner name are required."
    elif phone.startswith("91") and len(phone) == 12:
        phone = phone[2:]
    if error is None and len(phone) != 10:
        error = "Please enter a valid 10-digit phone number."
    elif error is None and area not in AREAS:
        error = "Please choose a valid area."
    elif error is None and category not in CATEGORIES:
        error = "Please choose a valid category."
    elif error is None and not re.fullmatch(r"[\w.\-]{2,}@[A-Za-z]{2,}", upi_id):
        error = "Please enter a valid UPI ID (example: name@upi)."

    if error:
        flash(error, "error")
        return redirect(url_for("vendor"))

    db = get_db()
    cur = db.execute(
        "INSERT INTO vendors (shop_name, owner_name, phone, area, category, upi_id) "
        "VALUES (?, ?, ?, ?, ?, ?)", (shop_name, owner_name, phone, area, category, upi_id))
    db.commit()
    session["vendor_id"] = cur.lastrowid
    flash(f"Welcome, {owner_name}! Your shop is now live.", "success")
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.pop("vendor_id", None)
    flash("You have been logged out.", "success")
    return redirect(url_for("vendor"))


# --------------------------------------------------------------------------
# Vendor dashboard
# --------------------------------------------------------------------------
@app.route("/dashboard")
def dashboard():
    vendor_row = current_vendor()
    if vendor_row is None:
        return redirect(url_for("vendor"))
    db = get_db()
    items = db.execute(
        "SELECT id, name, price FROM items WHERE vendor_id = ? ORDER BY id DESC",
        (vendor_row["id"],)).fetchall()
    sales = db.execute(
        "SELECT id, amount, mode, sale_date FROM sales WHERE vendor_id = ? "
        "ORDER BY sale_date DESC, id DESC LIMIT 50", (vendor_row["id"],)).fetchall()
    return render_template("dashboard.html", vendor=vendor_row, items=items,
                           sales=sales, stats=get_stats(vendor_row["id"]))


# --------------------------------------------------------------------------
# JSON API (used by fetch() in dashboard.html)
# --------------------------------------------------------------------------
@app.route("/api/items", methods=["POST"])
def api_add_item():
    vendor_row = api_vendor_or_401()
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:80]
    price = parse_positive_float(data.get("price"))
    if not name:
        return jsonify({"error": "Item name is required."}), 400
    if price is None:
        return jsonify({"error": "Enter a valid price greater than 0."}), 400

    db = get_db()
    cur = db.execute(
        "INSERT INTO items (vendor_id, name, category, price) VALUES (?, ?, ?, ?)",
        (vendor_row["id"], name, vendor_row["category"], price))
    db.commit()
    return jsonify({"item": {"id": cur.lastrowid, "name": name, "price": price}}), 201


@app.route("/api/items/<int:item_id>", methods=["DELETE"])
def api_delete_item(item_id):
    vendor_row = api_vendor_or_401()
    db = get_db()
    cur = db.execute("DELETE FROM items WHERE id = ? AND vendor_id = ?",
                     (item_id, vendor_row["id"]))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "Item not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/sales", methods=["POST"])
def api_add_sale():
    vendor_row = api_vendor_or_401()
    data = request.get_json(silent=True) or {}
    amount = parse_positive_float(data.get("amount"))
    mode = str(data.get("mode", "")).strip().title()
    if amount is None:
        return jsonify({"error": "Enter a valid amount greater than 0."}), 400
    if mode not in ("Cash", "Digital"):
        return jsonify({"error": "Mode must be Cash or Digital."}), 400

    sale_date = now_ist().strftime("%Y-%m-%d %H:%M:%S")
    db = get_db()
    cur = db.execute(
        "INSERT INTO sales (vendor_id, amount, mode, sale_date) VALUES (?, ?, ?, ?)",
        (vendor_row["id"], amount, mode, sale_date))
    db.commit()
    return jsonify({
        "sale": {"id": cur.lastrowid, "amount": amount, "mode": mode, "sale_date": sale_date},
        "stats": get_stats(vendor_row["id"]),
    }), 201


# --------------------------------------------------------------------------
# UPI QR code
# --------------------------------------------------------------------------
@app.route("/qr/<int:vid>")
def qr(vid):
    row = get_db().execute(
        "SELECT shop_name, upi_id FROM vendors WHERE id = ?", (vid,)).fetchone()
    if row is None:
        abort(404)

    upi_url = f"upi://pay?pa={quote(row['upi_id'], safe='@')}&pn={quote(row['shop_name'])}&cu=INR"
    amount = parse_positive_float(request.args.get("amt"))
    if amount is not None:
        upi_url += f"&am={amount:.2f}"

    qr_obj = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=10, border=3)
    qr_obj.add_data(upi_url)
    qr_obj.make(fit=True)
    img = qr_obj.make_image(fill_color="#581c87", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
