from datetime import UTC, datetime

from fastapi import FastAPI

from payment_gateway_api.app import create_app as create_gateway_app
from payment_gateway_api.bank import BankDecision
from payment_gateway_api.config import Settings
from payment_gateway_api.models import Payment, PaymentRequest
from payment_gateway_api.repository import InMemoryPaymentRepository

PAN_SENTINEL = "9999888877776661"
CVV_SENTINEL = "987"


def create_app() -> FastAPI:
    call_order: list[str] = []

    class AuthorizingBank:
        async def submit(self, payment: PaymentRequest) -> BankDecision:
            call_order.append("bank_authorized")
            return BankDecision(True, "runtime-authorization")

    class FailingRepository(InMemoryPaymentRepository):
        def save(self, payment: Payment) -> None:
            call_order.append("save_failed")
            raise RuntimeError(
                f"Repository failure: card_number={PAN_SENTINEL}, cvv={CVV_SENTINEL}"
            )

    repository = FailingRepository()
    application = create_gateway_app(
        settings=Settings(),
        bank=AuthorizingBank(),
        repository=repository,
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )

    @application.get("/__test__/state")
    async def state() -> dict[str, object]:
        return {"call_order": call_order, "stored_count": len(repository)}

    @application.get("/__test__/response-validation", response_model=Payment)
    async def invalid_response() -> dict[str, str]:
        return {"card_number": PAN_SENTINEL, "cvv": CVV_SENTINEL}

    return application
