"""Main payment processing logic."""

from payments.validators import validate_amount, PaymentError, validate_order
from payments.database import save_payment
from payments.decorators import retry
from payments.models import Order


@retry(max_attempts=3)
def process_payment(order: Order, amount: float, payment_method: str) -> bool:
    """Process a payment for an order.

    This is the entry point that calls validate_amount.
    Also initiates the 3-hop chain via save_payment.
    """
    try:
        validate_amount(amount)
        validate_order({"order_id": order.order_id, "customer_id": order.customer_id, "items": order.items})
        success = save_payment(f"pay_{order.order_id}", amount, order.order_id)
        if success:
            order.status = "paid"
        return success
    except PaymentError as e:
        raise PaymentError(f"Payment failed: {e}")


def process_refund(order_id: str, amount: float) -> bool:
    """Process a refund for an order.

    Also calls validate_amount (part of fan-in).
    """
    try:
        validate_amount(amount)
        print(f"Refunding {amount} for order {order_id}")
        return True
    except PaymentError:
        return False
