"""Validation logic for payments."""

from payments.config import MAX_AMOUNT, MIN_AMOUNT


class PaymentError(Exception):
    """Raised when a payment validation fails."""

    pass


def validate_amount(amount: float) -> bool:
    """Validate that amount is within acceptable range.

    This is called by multiple functions:
    - process_payment
    - log_transaction
    - SpecialOrder.validate
    - process_refund
    - etc.
    (fan-in symbol for testing)
    """
    if amount < MIN_AMOUNT:
        raise PaymentError(f"Amount {amount} is less than minimum {MIN_AMOUNT}")
    if amount > MAX_AMOUNT:
        raise PaymentError(f"Amount {amount} exceeds maximum {MAX_AMOUNT}")
    return True


def validate_order(order_dict: dict) -> bool:
    """Validate order structure."""
    required = ["order_id", "customer_id", "items"]
    if not all(k in order_dict for k in required):
        raise PaymentError(f"Missing required fields: {required}")
    return True
