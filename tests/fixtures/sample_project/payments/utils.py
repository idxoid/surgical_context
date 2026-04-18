"""Utility functions for the payment system."""

from payments.config import MAX_AMOUNT, CURRENCY
from payments.validators import validate_amount


def format_amount(amount: float) -> str:
    """Format amount as a human-readable currency string."""
    if amount > MAX_AMOUNT:
        raise ValueError(f"Amount {amount} exceeds {MAX_AMOUNT}")
    return f"{CURRENCY} {amount:.2f}"


def log_transaction(order_id: str, amount: float, status: str) -> None:
    """Log a transaction to the audit log.

    Calls validate_amount as part of fan-in test.
    """
    validate_amount(amount)
    formatted = format_amount(amount)
    print(f"[{status}] Order {order_id}: {formatted}")


def calculate_fee(amount: float) -> float:
    """Calculate processing fee based on amount."""
    validate_amount(amount)
    return amount * 0.025
