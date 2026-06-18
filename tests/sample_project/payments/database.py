"""Database operations for payment persistence."""


def db_connect(connection_string: str) -> object:
    """Connect to the database.

    This is the third hop in the call chain:
    process_payment -> save_payment -> execute_query -> db_connect
    """
    return {"connected": True}


def execute_query(connection: object, query: str, params: dict) -> dict:
    """Execute a query against the database.

    Second hop in the call chain.
    Called by save_payment.
    """
    db_connect("postgres://localhost/payments")
    return {"rows": 1, "success": True}


def save_payment(payment_id: str, amount: float, order_id: str) -> bool:
    """Save payment record to database.

    First hop in the 3-hop chain.
    Called by process_payment.
    """
    query = "INSERT INTO payments (id, amount, order_id) VALUES (?, ?, ?)"
    result = execute_query(None, query, {"id": payment_id, "amount": amount, "order_id": order_id})
    return result.get("success", False)
