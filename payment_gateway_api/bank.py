import asyncio
import math
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING

import httpx

from payment_gateway_api.errors import BankOutcomeUnknown, BankUnavailable

if TYPE_CHECKING:
    from payment_gateway_api.models import PaymentRequest


@dataclass(frozen=True)
class BankDecision:
    authorized: bool
    authorization_code: str | None


def _decision_from_response(response: httpx.Response) -> BankDecision:
    if response.status_code == 503:
        raise BankUnavailable()
    if response.status_code != 200:
        raise BankOutcomeUnknown()

    try:
        body = response.json()
    except ValueError:
        raise BankOutcomeUnknown() from None

    if not isinstance(body, dict) or type(body.get("authorized")) is not bool:
        raise BankOutcomeUnknown()

    if not body["authorized"]:
        return BankDecision(authorized=False, authorization_code=None)

    code = body.get("authorization_code")
    if not isinstance(code, str) or not code.strip():
        raise BankOutcomeUnknown()
    try:
        code.encode("utf-8")
    except UnicodeEncodeError:
        raise BankOutcomeUnknown() from None
    return BankDecision(authorized=True, authorization_code=code)


class BankClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        deadline_seconds: float = 3.0,
        observe: Callable[[str, float], None] | None = None,
    ) -> None:
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("Bank deadline must be finite and positive")
        self._client = client
        self._deadline_seconds = deadline_seconds
        self._observe = observe

    async def submit(self, payment: PaymentRequest) -> BankDecision:
        started = perf_counter()
        outcome = "error"
        try:
            try:
                async with asyncio.timeout(self._deadline_seconds):
                    response = await self._client.post(
                        "/payments",
                        json={
                            "card_number": payment.card_number,
                            "expiry_date": (
                                f"{payment.expiry_month:02d}/{payment.expiry_year:04d}"
                            ),
                            "currency": payment.currency,
                            "amount": payment.amount,
                            "cvv": payment.cvv,
                        },
                        follow_redirects=False,
                    )
            except httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout:
                raise BankUnavailable() from None
            except httpx.TimeoutException, TimeoutError:
                raise BankOutcomeUnknown(status_code=504) from None
            except httpx.RequestError:
                raise BankOutcomeUnknown() from None

            decision = _decision_from_response(response)
            outcome = "authorized" if decision.authorized else "declined"
            return decision
        except BankUnavailable:
            outcome = "unavailable"
            raise
        except BankOutcomeUnknown as exc:
            outcome = "timeout" if exc.status_code == 504 else "unknown"
            raise
        finally:
            if self._observe is not None:
                with suppress(Exception):
                    self._observe(outcome, perf_counter() - started)
