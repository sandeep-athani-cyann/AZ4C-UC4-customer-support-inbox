"""Read-only tools over the data pack. Agents call these through ToolBox so every call is traced.

Every tool takes plain values and returns plain dicts/lists. A tool raises ToolError when
the data it needs is missing; the orchestrator turns that into a handover.
"""
import re
import sqlite3
from datetime import date

from . import config

ORDER_RE = re.compile(r"\bLH-3\d{5}\b")


class ToolError(Exception):
    pass


def days_between(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _connect() -> sqlite3.Connection:
    path = config.db_path()
    if not path.is_file():
        raise ToolError(f"store.sqlite not found at {path}; set DATA_DIR to the data pack folder")
    # Read-only: the source data must never change.
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _rows(sql: str, params=()) -> list[dict]:
    con = _connect()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _header(text: str, field: str) -> str | None:
    m = re.search(rf"^\*\*{field}:\*\*\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else None


def get_email(email_id: str) -> dict:
    """The email split into its header, the latest message and the quoted earlier messages."""
    if not re.fullmatch(r"EM-\d{4}", email_id or ""):
        raise ToolError(f"bad email id {email_id!r}")
    path = config.docs_dir() / "emails" / f"{email_id}.md"
    if not path.is_file():
        raise ToolError(f"email {email_id} not found")
    text = path.read_text(encoding="utf-8")
    body = text.split("**Subject:**", 1)[-1].split("\n", 1)[-1]
    body = body.replace("*Synthetic and fictional data.*", "")
    latest, _, history = body.partition("\n---")
    quoted = []
    for block in re.split(r"\*\*On (\d{4}-\d{2}-\d{2}), ([^*]+?) wrote:\*\*", history)[1:]:
        quoted.append(block)
    earlier = []
    for i in range(0, len(quoted) - 2, 3):
        lines = [re.sub(r"^>\s?", "", ln) for ln in quoted[i + 2].splitlines()]
        earlier.append({"date": quoted[i], "from": quoted[i + 1].strip(), "body": "\n".join(lines).strip()})
    return {
        "email_id": _header(text, "Email ID") or email_id,
        "from": (_header(text, "From") or "").lower(),
        "to": _header(text, "To"),
        "date": _header(text, "Date"),
        "subject": _header(text, "Subject") or "",
        "body": latest.strip(),
        "earlier": earlier,
        "doc": f"docs/emails/{email_id}.md",
    }


def find_customer(email: str) -> dict | None:
    rows = _rows("SELECT customer_id, name, email, city FROM customers WHERE lower(email) = lower(?)", (email,))
    return rows[0] if rows else None


def get_order(order_id: str) -> dict:
    """Order with its owner's email, its shipment, items (with product data) and past refunds."""
    rows = _rows("""SELECT o.order_id, o.customer_id, o.order_date, o.status, o.payment_method,
                           c.email AS customer_email, c.name AS customer_name
                    FROM orders o LEFT JOIN customers c USING (customer_id) WHERE o.order_id = ?""", (order_id,))
    if not rows:
        raise ToolError(f"order {order_id} not found")
    order = rows[0]
    ship = _rows("SELECT courier, tracking_no, status, dispatched_on, delivered_on FROM shipments WHERE order_id = ?",
                 (order_id,))
    order["shipment"] = ship[0] if ship else None
    order["items"] = _rows("""SELECT i.sku, p.name, p.category, p.returnable, i.qty, i.unit_price
                              FROM order_items i LEFT JOIN products p USING (sku) WHERE i.order_id = ?""", (order_id,))
    order["refunds"] = _rows("SELECT refund_id, sku, amount, status, created_on FROM refunds WHERE order_id = ?",
                             (order_id,))
    return order


def get_customer_orders(customer_id: str) -> list[dict]:
    return _rows("SELECT order_id, order_date, status FROM orders WHERE customer_id = ? ORDER BY order_date DESC",
                 (customer_id,))


def list_product_names() -> list[str]:
    return [r["name"] for r in _rows("SELECT DISTINCT name FROM products")]


def list_help_articles() -> list[dict]:
    out = []
    for path in sorted((config.docs_dir() / "help_articles").glob("HA-*.md")):
        text = path.read_text(encoding="utf-8")
        m = re.match(r"#\s*(HA-\d+):\s*(.+)", text)
        body = text.split("\n", 1)[-1].replace("*Synthetic and fictional data.*", "").strip()
        out.append({"id": m.group(1) if m else path.stem, "title": m.group(2).strip() if m else path.stem,
                    "text": body})
    if not out:
        raise ToolError("no help articles found")
    return out


def get_policy_text(name: str) -> str:
    path = config.docs_dir() / "policies" / name
    if not path.is_file():
        raise ToolError(f"policy {name} not found")
    return path.read_text(encoding="utf-8")


TOOLS = {f.__name__: f for f in (get_email, find_customer, get_order, get_customer_orders, list_product_names,
                                 list_help_articles, get_policy_text)}


class ToolBox:
    """The only way agents reach the data. Logs each call to the trace and caches repeated calls."""

    def __init__(self, trace):
        self.trace = trace
        self._cache = {}

    def call(self, name: str, **args):
        key = (name, tuple(sorted(args.items())))
        if key in self._cache:
            return self._cache[key]
        result = self.trace.timed("tool", name, TOOLS[name], **args)
        self._cache[key] = result
        return result
