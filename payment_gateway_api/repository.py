from payment_gateway_api.models import Payment


class InMemoryPaymentRepository:
    def __init__(self) -> None:
        self._payments: dict[str, Payment] = {}

    def save(self, payment: Payment) -> None:
        if payment.id in self._payments:
            raise ValueError("A payment with this identifier already exists.")
        self._payments[payment.id] = payment

    def get(self, payment_id: str) -> Payment | None:
        return self._payments.get(payment_id)

    def __len__(self) -> int:
        return len(self._payments)
