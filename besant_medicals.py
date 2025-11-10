# besant_medicals_clean_v2.py
import sqlite3
from datetime import datetime
import streamlit as st
import pandas as pd
import io
import os
from pathlib import Path
import qrcode
from io import BytesIO
from fpdf import FPDF

# ---------- config ----------
MERCHANT_VPA = "snekhaganesh87@okhdfcbank"
MERCHANT_NAME = "Snekha Ganesh"
STATIC_QR_PATH = r"qr_snekha.png"
DB_PATH = "clinic_sqlite.db"

# ---------- DB helpers ----------
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def initialize_db():
    conn = get_conn()
    cur = conn.cursor()
    # Create basic tables if not exist (customers created with email/address optional)
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT NOT NULL UNIQUE
    );
    CREATE TABLE IF NOT EXISTS tablets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price REAL NOT NULL,
        stock INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        order_date TEXT NOT NULL,
        FOREIGN KEY(customer_id) REFERENCES customers(id)
    );
    CREATE TABLE IF NOT EXISTS order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        tablet_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        FOREIGN KEY(order_id) REFERENCES orders(id),
        FOREIGN KEY(tablet_id) REFERENCES tablets(id)
    );
    """)
    conn.commit()

    # Migration: add email/address columns to customers if missing
    cur.execute("PRAGMA table_info(customers)")
    cols = [r[1] for r in cur.fetchall()]
    if "email" not in cols:
        try:
            cur.execute("ALTER TABLE customers ADD COLUMN email TEXT")
        except Exception:
            pass
    if "address" not in cols:
        try:
            cur.execute("ALTER TABLE customers ADD COLUMN address TEXT")
        except Exception:
            pass
    conn.commit()
    conn.close()

SEED_TABLETS = [
    ("Paracetamol 500mg", 20.0, 100),
    ("Ibuprofen 200mg", 25.0, 80),
    ("Cetirizine 10mg", 15.0, 120),
    ("Amoxicillin 500mg", 60.0, 50),
    ("Multivitamin", 40.0, 60),
    ("Aspirin 75mg", 18.0, 70),
    ("Omeprazole 20mg", 30.0, 40),
    ("Azithromycin 250mg", 55.0, 30),
    ("Loratadine 10mg", 22.0, 90),
    ("Calcium + Vitamin D", 45.0, 50)
]

def sync_seed_tablets():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM tablets")
    existing = set(r[0] for r in cur.fetchall())
    to_insert = [s for s in SEED_TABLETS if s[0] not in existing]
    if to_insert:
        cur.executemany("INSERT INTO tablets (name, price, stock) VALUES (?, ?, ?)", to_insert)
        conn.commit()
    conn.close()

def fetch_tablets():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, price, stock FROM tablets ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "price": float(r[2]), "stock": r[3]} for r in rows]

def find_customer_by_phone(phone):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, email, address FROM customers WHERE phone = ?", (phone,))
    r = cur.fetchone()
    conn.close()
    return r

def create_or_update_customer(name, phone, email=None, address=None):
    conn = get_conn()
    cur = conn.cursor()
    # Try insert, if phone exists update email/address and name
    try:
        cur.execute("INSERT INTO customers (name, phone, email, address) VALUES (?, ?, ?, ?)", (name, phone, email, address))
        conn.commit()
        cid = cur.lastrowid
    except sqlite3.IntegrityError:
        # phone exists -> update
        cur.execute("SELECT id FROM customers WHERE phone = ?", (phone,))
        row = cur.fetchone()
        cid = row[0] if row else None
        cur.execute("UPDATE customers SET name = ?, email = ?, address = ? WHERE phone = ?", (name, email, address, phone))
        conn.commit()
    conn.close()
    return cid

def create_order(customer_id, items):
    conn = get_conn()
    cur = conn.cursor()
    order_date = datetime.now().isoformat(sep=' ', timespec='seconds')
    cur.execute("INSERT INTO orders (customer_id, order_date) VALUES (?, ?)", (customer_id, order_date))
    order_id = cur.lastrowid
    for tid, qty in items:
        cur.execute("INSERT INTO order_items (order_id, tablet_id, quantity) VALUES (?, ?, ?)", (order_id, tid, qty))
        cur.execute("UPDATE tablets SET stock = stock - ? WHERE id = ?", (qty, tid))
    conn.commit()
    conn.close()
    return order_id

def get_customer_orders(customer_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, order_date FROM orders WHERE customer_id = ? ORDER BY order_date DESC", (customer_id,))
    orders = []
    for oid, odate in cur.fetchall():
        cur.execute("""
            SELECT t.id, t.name, oi.quantity, t.price
            FROM order_items oi
            JOIN tablets t ON t.id = oi.tablet_id
            WHERE oi.order_id = ?
        """, (oid,))
        items = cur.fetchall()
        items_list = []
        total = 0.0
        for tid, name, qty, price in items:
            subtotal = qty * price
            items_list.append({"tablet_id": tid, "name": name, "quantity": qty, "price": float(price), "subtotal": subtotal})
            total += subtotal
        orders.append({"order_id": oid, "date": odate, "items": items_list, "total": total})
    conn.close()
    return orders

# ---------- session/cart ----------
def init_session():
    if "cart" not in st.session_state:
        st.session_state.cart = {}
    if "page" not in st.session_state:
        st.session_state.page = "Shop"  # Shop | Checkout | Receipt | Admin
    # Checkout inputs
    if "checkout_first" not in st.session_state:
        st.session_state.checkout_first = ""
    if "checkout_last" not in st.session_state:
        st.session_state.checkout_last = ""
    if "checkout_phone" not in st.session_state:
        st.session_state.checkout_phone = ""
    if "checkout_email" not in st.session_state:
        st.session_state.checkout_email = ""
    if "checkout_address" not in st.session_state:
        st.session_state.checkout_address = ""
    if "pending" not in st.session_state:
        st.session_state.pending = None
    if "last_order_id" not in st.session_state:
        st.session_state.last_order_id = None
    if "admin_logged_in" not in st.session_state:
        st.session_state.admin_logged_in = False

def add_to_cart(tid, qty):
    if qty <= 0:
        return
    key = str(tid)
    st.session_state.cart[key] = st.session_state.cart.get(key, 0) + qty
    st.success("Added to cart")

def remove_from_cart(tid):
    st.session_state.cart.pop(str(tid), None)

def update_cart(tid, qty):
    if qty <= 0:
        remove_from_cart(tid)
    else:
        st.session_state.cart[str(tid)] = qty

def cart_details():
    tablets = {t['id']: t for t in fetch_tablets()}
    items = []
    total = 0.0
    for tid_s, qty in st.session_state.cart.items():
        tid = int(tid_s)
        t = tablets.get(tid)
        if not t:
            continue
        subtotal = qty * t['price']
        items.append({"tablet_id": tid, "name": t['name'], "qty": qty, "price": t['price'], "subtotal": subtotal, "stock": t['stock']})
        total += subtotal
    return items, total

# ---------- QR and PDF helpers ----------
def generate_upi_qr_bytes(vpa, payee_name, amount, note=None):
    upi = f"upi://pay?pa={vpa}&pn={payee_name}&am={amount:.2f}"
    if note:
        upi += f"&tn={note}"
    try:
        qr = qrcode.QRCode(box_size=6, border=2)
        qr.add_data(upi)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf.read(), None
    except Exception as e:
        return None, str(e)

def build_receipt_pdf_bytes(order_info):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, "Besant medicals - Receipt", ln=1, align="C")
    pdf.ln(4)
    pdf.set_font("Arial", size=11)
    pdf.cell(0, 6, f"Order ID: {order_info['order_id']}", ln=1)
    pdf.cell(0, 6, f"Date: {order_info['date']}", ln=1)
    pdf.cell(0, 6, f"Customer: {order_info['customer_name']}  |  Phone: {order_info['phone']}", ln=1)
    if order_info.get("email"):
        pdf.cell(0, 6, f"Email: {order_info['email']}", ln=1)
    if order_info.get("address"):
        pdf.multi_cell(0, 6, f"Address: {order_info['address']}")
    pdf.ln(6)
    pdf.set_font("Arial", "B", 11)
    pdf.cell(80, 8, "Tablet", border=1)
    pdf.cell(30, 8, "Qty", border=1, align="C")
    pdf.cell(40, 8, "Price (Rs)", border=1, align="R")
    pdf.cell(40, 8, "Subtotal (Rs)", border=1, align="R")
    pdf.ln()
    pdf.set_font("Arial", size=11)
    for it in order_info["items"]:
        pdf.cell(80, 8, it["name"], border=1)
        pdf.cell(30, 8, str(it["quantity"]), border=1, align="C")
        pdf.cell(40, 8, f"{it['price']:.2f}", border=1, align="R")
        pdf.cell(40, 8, f"{it['subtotal']:.2f}", border=1, align="R")
        pdf.ln()
    pdf.ln(2)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(150, 8, "Total", border=0, align="R")
    pdf.cell(40, 8, f"Rs {order_info['total']:.2f}", border=0, align="R")
    pdf.ln(10)
    pdf.set_font("Arial", size=11)
    pdf.cell(0, 6, f"Payment method: {order_info.get('method', 'Cash')}", ln=1)
    return pdf.output(dest='S').encode('latin-1')

# ---------- UI ----------
st.set_page_config(page_title="Besant medicals", layout="wide")
initialize_db()
sync_seed_tablets()
init_session()

# Sidebar nav
with st.sidebar:
    st.title("Navigate")
    page = st.radio("", ["Shop", "Checkout", "Receipt", "Admin"], index=["Shop","Checkout","Receipt","Admin"].index(st.session_state.page))
    st.session_state.page = page

# Centered title
st.markdown("<h1 style='text-align:center;color:blue'>Besant medicals</h1>", unsafe_allow_html=True)

# --- SHOP: single table-like rows ---
if st.session_state.page == "Shop":
    st.header("Available Tablets")
    tablets = fetch_tablets()
    c1, c2, c3, c4, c5 = st.columns([4,1,1,1,1])
    c1.markdown("**Tablet name**")
    c2.markdown("**Stock**")
    c3.markdown("**Price (Rs)**")
    c4.markdown("**Quantity**")
    c5.markdown("**Add**")
    for t in tablets:
        col_name, col_stock, col_price, col_qty, col_add = st.columns([4,1,1,1,1])
        with col_name:
            st.write(t['name'])
        with col_stock:
            st.write(t['stock'])
        with col_price:
            st.write(f"{t['price']:.2f}")
        qty_key = f"qty_{t['id']}"
        with col_qty:
            qty = st.number_input("", min_value=0, max_value=t['stock'], value=0, step=1, key=qty_key, label_visibility="collapsed")
        with col_add:
            if st.button("Add to cart", key=f"add_{t['id']}"):
                if qty <= 0:
                    st.warning("Choose quantity > 0")
                elif qty > t['stock']:
                    st.warning("Quantity exceeds stock")
                else:
                    add_to_cart(t['id'], int(qty))
                    st.rerun()

    st.write("")
    if st.button("Go to Checkout"):
        st.session_state.page = "Checkout"
        st.rerun()

# --- CHECKOUT (cart + checkout) ---
if st.session_state.page == "Checkout":
    st.header("Cart / Checkout")
    items, total = cart_details()
    if not items:
        st.info("Cart is empty.")
    else:
        df = pd.DataFrame([{"Name": it['name'], "Qty": it['qty'], "Price": it['price'], "Subtotal": it['subtotal']} for it in items])
        st.table(df)
        st.markdown(f"**Total: Rs {total:.2f}**")
        col_clear, col_additems = st.columns([1,1])
        with col_clear:
            if st.button("Clear cart"):
                st.session_state.cart = {}
                st.rerun()
        with col_additems:
            if st.button("Add items from previous order to cart"):
                # dummy: in real UI you'd choose which order — here we just show the idea
                st.info("Use Lookup by phone to add specific previous order items (use Lookup below).")

    st.markdown("---")
    st.subheader("Checkout details (for delivery / receipt)")
    # Lookup previous orders by phone
    phone_lookup = st.text_input("Lookup previous orders by phone (enter phone and press Lookup)", value="", key="lookup_phone")
    if st.button("Lookup"):
        phone = phone_lookup.strip()
        if not phone:
            st.warning("Enter phone first.")
        else:
            found = find_customer_by_phone(phone)
            if not found:
                st.info("No customer found with that phone.")
            else:
                cid = found[0]
                orders = get_customer_orders(cid)
                if not orders:
                    st.info("No previous orders.")
                else:
                    st.success(f"Found previous orders for {found[1]}")
                    for o in orders:
                        with st.expander(f"Order {o['order_id']} — {o['date']} — Rs {o['total']:.2f}"):
                            df_o = pd.DataFrame(o['items'])
                            st.table(df_o[['name','quantity','price','subtotal']].rename(columns={'name':'Tablet','quantity':'Qty','price':'Price','subtotal':'Subtotal'}))
                            if st.button(f"Add items from Order {o['order_id']} to cart", key=f"reorder_{o['order_id']}"):
                                tablets_map = {t['id']: t for t in fetch_tablets()}
                                added = False
                                for it in o['items']:
                                    tid = it['tablet_id']
                                    want = it['quantity']
                                    tcur = tablets_map.get(tid)
                                    if not tcur or tcur['stock'] <= 0:
                                        continue
                                    take = min(want, tcur['stock'])
                                    st.session_state.cart[str(tid)] = st.session_state.cart.get(str(tid), 0) + take
                                    added = True
                                if added:
                                    st.success("Items from previous order added to cart.")
                                    st.rerun()
                                else:
                                    st.info("No items available to add from that order.")

    colf, coll = st.columns(2)
    with colf:
        st.text_input("First name", key="checkout_first")
    with coll:
        st.text_input("Last name", key="checkout_last")
    st.text_input("Phone", key="checkout_phone", placeholder="Enter phone number")
    st.text_input("Email (optional)", key="checkout_email", placeholder="Enter email for receipt")
    st.text_area("Delivery address (for COD/delivery)", key="checkout_address", placeholder="Enter full address (house, street, city, pin)")

    payment = st.selectbox("Payment method", ["UPI (scan QR)", "Cash on delivery (COD)"])

    if st.button("Proceed to payment"):
        if not st.session_state.checkout_phone.strip():
            st.error("Phone is required")
        elif not items:
            st.error("Cart is empty")
        else:
            current_tablets = {t['id']: t for t in fetch_tablets()}
            final_items = []
            for it in items:
                tid = it['tablet_id']
                want = it['qty']
                tcur = current_tablets.get(tid)
                if not tcur or tcur['stock'] <= 0:
                    st.warning(f"{it['name']} out of stock; skipped.")
                    continue
                take = min(want, tcur['stock'])
                final_items.append((tid, take))
            if not final_items:
                st.error("No items available to place order")
            else:
                name_full = f"{st.session_state.checkout_first.strip()} {st.session_state.checkout_last.strip()}".strip()
                if not name_full:
                    name_full = "Unknown"
                st.session_state.pending = {
                    "items": final_items,
                    "total": total,
                    "first": st.session_state.checkout_first.strip(),
                    "last": st.session_state.checkout_last.strip(),
                    "name": name_full,
                    "phone": st.session_state.checkout_phone.strip(),
                    "email": st.session_state.checkout_email.strip(),
                    "address": st.session_state.checkout_address.strip(),
                    "method": payment
                }
                st.success("Prepared payment. Scroll down to complete payment.")
                st.rerun()

    # if pending prepared, show payment UI and single confirm button
    if st.session_state.pending:
        pending = st.session_state.pending
        st.markdown("---")
        st.subheader("Payment")
        st.write(f"Customer: **{pending['name']}**  |  Phone: **{pending['phone']}**")
        if pending.get("email"):
            st.write(f"Email: **{pending['email']}**")
        if pending.get("address"):
            st.write(f"Address: **{pending['address']}**")
        st.write(f"Amount: **Rs {pending['total']:.2f}**")
        if pending.get("method","").startswith("UPI"):
            qr_bytes, err = generate_upi_qr_bytes(MERCHANT_VPA, MERCHANT_NAME, pending['total'])
            if qr_bytes:
                st.image(qr_bytes, width=220, caption=f"Scan to pay Rs {pending['total']:.2f} via UPI")
            else:
                st.warning("Could not generate dynamic QR. Showing static if present.")
                if os.path.exists(STATIC_QR_PATH):
                    st.image(STATIC_QR_PATH, caption="Static QR")
                else:
                    st.error("No QR available.")
        else:
            st.info("Cash on delivery selected. Collect cash on delivery from the customer and then press Confirm payment.")

        if st.button("Confirm payment (simulate)"):
            # store / update customer
            cid = create_or_update_customer(pending['name'], pending['phone'], pending.get('email'), pending.get('address'))
            order_id = create_order(cid, pending['items'])
            tablets_map = {t['id']: t for t in fetch_tablets()}
            items_for_receipt = []
            total_calc = 0.0
            for tid, qty in pending['items']:
                t = tablets_map.get(tid)
                price = t['price'] if t else 0.0
                subtotal = price * qty
                items_for_receipt.append({"name": t['name'] if t else "Unknown", "quantity": qty, "price": price, "subtotal": subtotal})
                total_calc += subtotal
            receipt = {
                "order_id": order_id,
                "date": datetime.now().isoformat(sep=' ', timespec='seconds'),
                "customer_name": pending['name'],
                "first": pending.get('first'),
                "last": pending.get('last'),
                "phone": pending['phone'],
                "email": pending.get('email'),
                "address": pending.get('address'),
                "items": items_for_receipt,
                "total": total_calc,
                "method": pending.get("method", "Cash")
            }
            st.session_state.pending = receipt
            st.session_state.last_order_id = order_id
            st.session_state.cart = {}
            for k in ["checkout_first", "checkout_last", "checkout_phone", "checkout_email", "checkout_address"]:
                if k in st.session_state:
                    del st.session_state[k]
            st.success(f"Order placed. Order ID: {order_id}")
            st.session_state.page = "Receipt"
            st.rerun()

# --- RECEIPT PAGE ---
if st.session_state.page == "Receipt":
    st.header("Receipt")
    receipt = st.session_state.pending
    if not receipt:
        st.info("No receipt to show. Complete an order first.")
    else:
        # Payment success box
        st.success(f"Payment successful — Order ID: {receipt['order_id']}")
        st.subheader(f"Order {receipt['order_id']} — {receipt['date']}")
        st.write(f"Customer: **{receipt['customer_name']}**  |  Phone: **{receipt['phone']}**")
        if receipt.get("email"):
            st.write(f"Email: **{receipt['email']}**")
        if receipt.get("address"):
            st.write(f"Address: **{receipt['address']}**")
        st.write(f"Payment method: **{receipt.get('method','Cash')}**")
        df = pd.DataFrame(receipt["items"])
        st.table(df.rename(columns={"name":"Tablet","quantity":"Qty","price":"Price","subtotal":"Subtotal"})[['Tablet','Qty','Price','Subtotal']])
        st.markdown(f"**Total: Rs {receipt['total']:.2f}**")
        st.markdown("---")
        qr_bytes = None
        if receipt.get("method", "").startswith("UPI"):
            qr_bytes, err = generate_upi_qr_bytes(MERCHANT_VPA, MERCHANT_NAME, receipt['total'])
            if qr_bytes:
                st.image(qr_bytes, width=220, caption=f"Scan to pay Rs {receipt['total']:.2f}")
            elif os.path.exists(STATIC_QR_PATH):
                st.image(STATIC_QR_PATH, caption="Static QR")
        elif receipt.get("method", "").startswith("Cash"):
            st.info("Payment done via Cash on Delivery.")

        pdf_bytes = build_receipt_pdf_bytes(receipt)
        st.download_button("Download receipt (PDF)", data=pdf_bytes, file_name=f"receipt_{receipt['order_id']}.pdf", mime="application/pdf")

# --- ADMIN (simple password) ---
if st.session_state.page == "Admin":
    st.header("Admin — Restock & DB view")
    if not st.session_state.admin_logged_in:
        pwd = st.text_input("Enter admin password to continue", type="password")
        if st.button("Login"):
            # simple password check (change to secure in real app)
            if pwd == "admin123":
                st.session_state.admin_logged_in = True
                st.success("Admin logged in")
                st.rerun()
            else:
                st.error("Wrong password")
    else:
        st.subheader("Tablets (edit stock)")
        tablets = fetch_tablets()
        for t in tablets:
            cols = st.columns([3,1,1,1])
            cols[0].write(t['name'])
            cols[1].write(f"Price: Rs {t['price']:.2f}")
            cols[2].write(f"Stock: {t['stock']}")
            add_key = f"restock_{t['id']}"
            qty = cols[3].number_input("Add qty", min_value=0, value=0, step=1, key=add_key, label_visibility="collapsed")
            if cols[3].button("Update stock", key=f"update_{t['id']}"):
                if qty > 0:
                    conn = get_conn()
                    cur = conn.cursor()
                    cur.execute("UPDATE tablets SET stock = stock + ? WHERE id = ?", (qty, t['id']))
                    conn.commit()
                    conn.close()
                    st.success(f"Added {qty} to {t['name']}")
                    st.rerun()
                else:
                    st.warning("Enter qty > 0")

        st.markdown("---")
        st.subheader("Database viewer (customers & orders)")
        if st.button("Show customers"):
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("SELECT id, name, phone, email, address FROM customers")
            rows = cur.fetchall()
            conn.close()
            if rows:
                df = pd.DataFrame(rows, columns=["id","name","phone","email","address"])
                st.dataframe(df)
            else:
                st.info("No customers yet.")
        if st.button("Show orders (all)"):
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("SELECT o.id, o.order_date, c.name, c.phone FROM orders o JOIN customers c ON c.id = o.customer_id ORDER BY o.order_date DESC")
            rows = cur.fetchall()
            conn.close()
            if rows:
                df = pd.DataFrame(rows, columns=["order_id","date","customer","phone"])
                st.dataframe(df)
            else:
                st.info("No orders yet.")

        if st.button("Logout admin"):
            st.session_state.admin_logged_in = False
            st.success("Logged out")
            st.rerun()
