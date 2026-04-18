"""Data models for the payment system."""

from dataclasses import dataclass
from payments.config import MAX_AMOUNT, CURRENCY


@dataclass
class LineItem:
    description: str
    amount: float
    quantity: int

    def total(self) -> float:
        return self.amount * self.quantity


@dataclass
class Order:
    order_id: str
    customer_id: str
    items: list[LineItem]
    status: str = "pending"

    def total_amount(self) -> float:
        total = sum(item.total() for item in self.items)
        if total > MAX_AMOUNT:
            raise ValueError(f"Order exceeds maximum amount {MAX_AMOUNT}")
        return total

    def validate(self) -> bool:
        return len(self.items) > 0 and self.total_amount() > 0
