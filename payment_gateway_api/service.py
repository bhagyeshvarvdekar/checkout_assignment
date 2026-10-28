from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from payment_gateway_api.errors import (
    GatewayError,
    PaymentNotFound,
    PaymentProcessingError,
    PaymentRejected,
)
from payment_gateway_api.models import Payment, PaymentRequest
from payment_gateway_api.repository import InMemoryPaymentRepository

if TYPE_CHECKING:
    from payment_gateway_api.bank import BankDecision


class Bank(Protocol):
    async def submit(self, payment: PaymentRequest) -> BankDecision: ...


class PaymentService:
    def __init__(
        self,
        bank: Bank,
        repository: InMemoryPaymentRepository,
        clock: Callable[[], datetime],
    ) -> None:
        self._bank = bank
        self._repository = repository
        self._clock = clock

    async def create(self, request: PaymentRequest) -> Payment:
        try:
            now = self._clock()
            if now.utcoffset() is None:
                raise ValueError("The payment clock must return an aware datetime.")
            now = now.astimezone(UTC)
            if (request.expiry_year, request.expiry_month) < (now.year, now.month):
                raise PaymentRejected(
                    [
                        {
                            "field": "expiry_month",
                            "message": "Card expiry must be the current month or later.",
                        }
                    ]
                )

            decision = await self._bank.submit(request)
            payment = Payment(
                id=str(uuid4()),
                status="Authorized" if decision.authorized else "Declined",
                card_number_last_four=request.card_number[-4:],
                expiry_month=request.expiry_month,
                expiry_year=request.expiry_year,
                currency=request.currency,
                amount=request.amount,
                authorization_code=(decision.authorization_code if decision.authorized else None),
            )
            self._repository.save(payment)
            return payment
        except GatewayError:
            raise
        except Exception:
            raise PaymentProcessingError() from None

    def get(self, payment_id: str) -> Payment:
        try:
            payment = self._repository.get(payment_id)
            if payment is None:
                raise PaymentNotFound()
            return payment
        except GatewayError:
            raise
        except Exception:
            raise PaymentProcessingError() from None
