"""Specialized order handling."""

from payments.models import Order
from payments.validators import validate_amount


class SpecialOrder(Order):
    """Order type with special discounts and validations.

    Inherits from Order (cross-file inheritance → DEPENDS_ON edge).
    """

    discount_percentage: float = 0.0

    def apply_discount(self, percentage: float) -> None:
        """Apply a percentage discount to the order."""
        self.discount_percentage = percentage

    def validate(self) -> bool:
        """Validate order including custom checks.

        Calls validate_amount as part of validation logic.
        """
        if not super().validate():
            return False
        total = self.total_amount()
        validate_amount(total)
        return True

    def final_amount(self) -> float:
        """Calculate final amount after discount."""
        total = self.total_amount()
        discount = total * (self.discount_percentage / 100)
        return total - discount
