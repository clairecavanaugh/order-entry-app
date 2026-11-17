"""
app.py - Streamlit Order Entry App (DATA 4000 - Python with AI)

Run locally:   streamlit run app.py

How the order flow avoids duplicate orders
------------------------------------------
Streamlit reruns this whole script every time a widget changes. To make sure
a rerun never creates an order:
  1. Clicking "Review order" creates a review with a unique token and stores
     it in st.session_state. Nothing is saved yet.
  2. The confirmation checkbox is tied to that token, so a new review always
     starts unconfirmed.
  3. The insert only happens inside the "Submit order" button branch. A
     button is True only on the single rerun right after it is clicked.
  4. The token is recorded as submitted before inserting, and the review is
     cleared afterwards, so the same review can never be inserted twice.
"""

import hmac
import uuid
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import database as db
from config import get_setting

st.set_page_config(page_title="Order Entry", page_icon="🛒", layout="wide")

LOCAL_TZ = ZoneInfo("America/New_York")  # Streamlit Cloud servers run on UTC
MONEY = st.column_config.NumberColumn(format="$%.2f")


# ---------------------------------------------------------------------------
# 0. Access control - runs BEFORE any database access
# ---------------------------------------------------------------------------
def require_password() -> None:
    expected = get_setting("APP_PASSWORD")
    if not expected:
        st.error("This app is not configured with an access password. "
                 "Add APP_PASSWORD to the app's secrets.")
        st.stop()

    if st.session_state.get("authenticated"):
        return

    st.title("🛒 Order Entry App")
    st.write("This app is restricted. Enter the access password to continue.")
    with st.form("login"):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Enter")
    if submitted:
        # compare_digest compares in constant time (avoids timing attacks)
        if hmac.compare_digest(password.encode(), expected.encode()):
            st.session_state.authenticated = True
            st.rerun()
        st.error("Incorrect password.")
    st.stop()  # nothing below this line runs until the password is correct


require_password()


# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------
st.session_state.setdefault("review", None)            # order awaiting confirmation
st.session_state.setdefault("submitted_tokens", set())  # reviews already inserted
st.session_state.setdefault("last_result", None)       # outcome of last submission
st.session_state.setdefault("flash", None)             # one-time notice after rerun


# ---------------------------------------------------------------------------
# Cached data loaders (cleared by Refresh Data or after a new order)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner="Loading customers from Supabase...")
def load_customers():
    return db.fetch_customers()


@st.cache_data(ttl=300, show_spinner="Loading products from Supabase...")
def load_products():
    return db.fetch_products()


@st.cache_data(ttl=300, show_spinner="Loading order history from Supabase...")
def load_orders():
    return db.fetch_orders()


def clear_cache():
    load_customers.clear()
    load_products.clear()
    load_orders.clear()


def money(value) -> str:
    return f"${Decimal(str(value)):,.2f}"


def today() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("🛒 Order Entry App")
st.write(
    "Select a customer and a product, enter a quantity, review the details, "
    "and save the order to the Supabase database. Every saved order is read "
    "back from the database to verify it was stored correctly."
)

if st.button("🔄 Refresh Data"):
    clear_cache()
    st.session_state.flash = ("success", "Data refreshed from Supabase.")
    st.rerun()

if st.session_state.flash:
    kind, message = st.session_state.flash
    getattr(st, kind)(message)
    st.session_state.flash = None

try:
    customers = load_customers()
    products = load_products()
except db.DatabaseError as exc:
    st.error(str(exc))
    st.stop()

customer_names = {c["customer_id"]: c["customer_name"] for c in customers}
product_lookup = {p["product_id"]: p for p in products}


# ---------------------------------------------------------------------------
# 1. Display customers and products
# ---------------------------------------------------------------------------
st.header("1. Customers and Products")
left, right = st.columns(2)
with left:
    st.subheader("Customers")
    st.dataframe(
        pd.DataFrame(customers).rename(
            columns={"customer_id": "Customer ID", "customer_name": "Customer Name"}
        ),
        hide_index=True, width="stretch",
    )
with right:
    st.subheader("Products")
    product_df = pd.DataFrame(products).rename(columns={
        "product_id": "Product ID", "product_name": "Product Name",
        "category": "Category", "price": "Price",
    })
    if not product_df.empty:
        product_df["Price"] = product_df["Price"].astype(float)
    st.dataframe(product_df, hide_index=True, width="stretch",
                 column_config={"Price": MONEY})


# ---------------------------------------------------------------------------
# 2. Order entry
# ---------------------------------------------------------------------------
st.header("2. Enter an Order")
c1, c2, c3 = st.columns([2, 2, 1])
with c1:
    customer_id = st.selectbox(
        "Customer", options=list(customer_names), index=None,
        placeholder="Choose a customer",
        format_func=lambda cid: f"{customer_names[cid]} (ID {cid})",
    )
with c2:
    product_id = st.selectbox(
        "Product", options=list(product_lookup), index=None,
        placeholder="Choose a product",
        format_func=lambda pid: (
            f"{product_lookup[pid]['product_name']} - {money(product_lookup[pid]['price'])}"
        ),
    )
with c3:
    # min_value=1 and step=1 (integers) block zero, negatives and decimals
    quantity = st.number_input("Quantity", min_value=1, max_value=10_000, value=1, step=1)

if st.button("Review order"):
    errors = []
    if customer_id is None:
        errors.append("Select a customer.")
    if product_id is None:
        errors.append("Select a product.")
    try:
        quantity = db.validate_quantity(quantity)
    except ValueError as exc:
        errors.append(str(exc))

    if errors:
        for e in errors:
            st.error(e)
    else:
        try:
            # Fresh lookups so the review uses the CURRENT database values
            customer = db.get_customer(customer_id)
            product = db.get_product(product_id)
        except db.DatabaseError as exc:
            st.error(str(exc))
        else:
            if customer is None or product is None:
                st.error("That customer or product no longer exists. Click Refresh Data.")
            else:
                st.session_state.review = {
                    "token": uuid.uuid4().hex,
                    "customer_id": customer_id,
                    "customer_name": customer["customer_name"],
                    "product_id": product_id,
                    "product_name": product["product_name"],
                    "quantity": quantity,
                    "unit_price": str(product["price"]),
                    "price_changed_from": None,
                }


def submit_order(review: dict) -> None:
    """Insert the reviewed order, then read it back for verification."""
    token = review["token"]

    if token in st.session_state.submitted_tokens:
        st.warning("This order was already submitted.")
        return
    if not st.session_state.get(f"confirm_{token}"):
        st.error("Please confirm the order before submitting.")
        return

    try:
        qty = db.validate_quantity(review["quantity"])
        product = db.get_product(review["product_id"])
        customer = db.get_customer(review["customer_id"])
    except ValueError as exc:
        st.error(str(exc))
        return
    except db.DatabaseError as exc:
        st.error(f"The order was NOT saved. {exc}")
        return
    if product is None or customer is None:
        st.error("The order was NOT saved: the customer or product no longer exists.")
        return

    # Price check: if it changed since review, show the new price and require
    # a fresh confirmation (new token -> new, unchecked checkbox).
    current_price = product["price"]
    if current_price != Decimal(review["unit_price"]):
        st.session_state.review = {
            **review,
            "token": uuid.uuid4().hex,
            "unit_price": str(current_price),
            "price_changed_from": review["unit_price"],
        }
        st.rerun()

    # Mark as submitted BEFORE inserting so no rerun can insert it again.
    st.session_state.submitted_tokens.add(token)
    try:
        order_id = db.insert_order(
            review["customer_id"], review["product_id"], qty, current_price, today()
        )
    except db.DatabaseError as exc:
        st.session_state.submitted_tokens.discard(token)  # allow a manual retry
        st.error(f"The order was NOT saved. {exc} "
                 "Check Order History before trying again.")
        return

    # Insert succeeded
    st.session_state.review = None
    load_orders.clear()
    st.session_state.last_result = verify_order(order_id)
    st.rerun()


def verify_order(order_id) -> dict:
    """Make NEW API queries to retrieve the saved order and its names."""
    if order_id is None:
        return {"status": "unverified", "order_id": None}
    try:
        saved = db.fetch_order(order_id)
        if saved is None:
            return {"status": "unverified", "order_id": order_id}
        customer = db.get_customer(saved["customer_id"])
        product = db.get_product(saved["product_id"])
    except db.DatabaseError:
        return {"status": "unverified", "order_id": order_id}

    unit_price = db.to_money(saved["unit_price"])
    return {
        "status": "verified",
        "order_id": order_id,
        "row": {
            "Order ID": saved["order_id"],
            "Customer": customer["customer_name"] if customer else f"ID {saved['customer_id']}",
            "Product": product["product_name"] if product else f"ID {saved['product_id']}",
            "Quantity": saved["quantity"],
            "Unit Price": float(unit_price),
            "Total": float(unit_price * saved["quantity"]),  # uses STORED unit_price
            "Order Date": saved["order_date"],
        },
    }


review = st.session_state.review
if review:
    st.subheader("Review your order")
    stale = (customer_id, product_id, quantity) != (
        review["customer_id"], review["product_id"], review["quantity"]
    )
    if stale:
        st.info("Your selections changed after review. Click **Review order** again.")
    if review["price_changed_from"]:
        st.warning(
            f"The price of {review['product_name']} changed from "
            f"{money(review['price_changed_from'])} to {money(review['unit_price'])}. "
            "Please review the updated total and confirm again."
        )

    unit_price = Decimal(review["unit_price"])
    st.dataframe(
        pd.DataFrame([{
            "Customer": review["customer_name"],
            "Product": review["product_name"],
            "Quantity": review["quantity"],
            "Unit Price": float(unit_price),
            "Total": float(unit_price * review["quantity"]),
        }]),
        hide_index=True, width="stretch",
        column_config={"Unit Price": MONEY, "Total": MONEY},
    )
    confirmed = st.checkbox("I confirm these order details are correct.",
                            key=f"confirm_{review['token']}", disabled=stale)
    if st.button("Submit order", type="primary", disabled=stale or not confirmed,
                 key=f"submit_{review['token']}"):
        submit_order(review)


# ---------------------------------------------------------------------------
# 3. Verification of the last saved order
# ---------------------------------------------------------------------------
result = st.session_state.last_result
if result:
    st.header("3. Saved Order Verification")
    if result["status"] == "verified":
        st.success(f"Order {result['order_id']} was saved and verified from Supabase.")
        st.dataframe(pd.DataFrame([result["row"]]), hide_index=True,
                     width="stretch",
                     column_config={"Unit Price": MONEY, "Total": MONEY})
    else:
        id_text = f" (order ID {result['order_id']})" if result["order_id"] else ""
        st.warning(
            f"The order was saved{id_text}, but it could not be retrieved for "
            "verification. Do not submit it again - check Order History or "
            "click Refresh Data."
        )


# ---------------------------------------------------------------------------
# 4. Order history (with customer filter - extra credit)
# ---------------------------------------------------------------------------
st.header("4. Order History")
try:
    orders = load_orders()
except db.DatabaseError as exc:
    st.error(str(exc))
    orders = None

if orders is not None:
    history_filter = st.selectbox(
        "Filter by customer", options=["ALL"] + list(customer_names),
        format_func=lambda cid: "All customers" if cid == "ALL"
        else f"{customer_names[cid]} (ID {cid})",
    )
    if history_filter != "ALL":
        orders = [o for o in orders if o["customer_id"] == history_filter]

    if not orders:
        st.info("No orders found.")
    else:
        history = pd.DataFrame([{
            "Order ID": o["order_id"],
            "Customer": customer_names.get(o["customer_id"], f"ID {o['customer_id']}"),
            "Product": product_lookup.get(o["product_id"], {}).get(
                "product_name", f"ID {o['product_id']}"),
            "Quantity": o["quantity"],
            "Unit Price": float(db.to_money(o["unit_price"])),
            "Total": float(db.to_money(o["unit_price"]) * o["quantity"]),
            "Order Date": o["order_date"],
        } for o in orders])
        st.dataframe(history, hide_index=True, width="stretch",
                     column_config={"Unit Price": MONEY, "Total": MONEY})

        total_value = sum(db.to_money(o["unit_price"]) * o["quantity"] for o in orders)
        label = ("Total value of all orders" if history_filter == "ALL"
                 else f"Total value of {customer_names[history_filter]}'s orders")
        st.metric(label, money(total_value))
