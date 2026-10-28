import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from payment_gateway_api.bank import BankClient, BankDecision
from payment_gateway_api.errors import BankOutcomeUnknown, BankUnavailable
from payment_gateway_api.models import PaymentRequest
from payment_gateway_api.repository import InMemoryPaymentRepository
from payment_gateway_api.service import PaymentService

pytestmark = pytest.mark.asyncio

PAN = "0222240534324887"
CVV = "012"
NOW = datetime(2026, 10, 28, tzinfo=UTC)


@pytest.fixture
def payment():
    return PaymentRequest(
        card_number=PAN,
        expiry_month=4,
        expiry_year=2030,
        currency="USD",
        amount=1050,
        cvv=CVV,
    )


@pytest.fixture
def repository():
    return InMemoryPaymentRepository()


async def test_authorization_sends_exact_bank_contract(payment):
    requests = []
    observations = []
    code = " référence-💳 "

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"authorized": True, "authorization_code": code})

    async with httpx.AsyncClient(
        base_url="http://bank.test", transport=httpx.MockTransport(handler)
    ) as client:
        bank = BankClient(client, observe=lambda *event: observations.append(event))
        decision = await bank.submit(payment)
        assert not client.is_closed

    assert decision == BankDecision(True, code)
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url == "http://bank.test/payments"
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert body == {
        "card_number": PAN,
        "expiry_date": "04/2030",
        "currency": "USD",
        "amount": 1050,
        "cvv": CVV,
    }
    assert type(body["amount"]) is int
    assert all(type(body[key]) is str for key in body if key != "amount")
    assert len(observations) == 1
    assert observations[0][0] == "authorized"
    assert observations[0][1] >= 0


async def test_decline_normalizes_the_simulators_empty_code(payment):
    async with httpx.AsyncClient(
        base_url="http://bank.test",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"authorized": False, "authorization_code": ""})
        ),
    ) as client:
        assert await BankClient(client).submit(payment) == BankDecision(False, None)


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(200, json=[]), id="non-object"),
        pytest.param(httpx.Response(200, json={"authorized": 1}), id="numeric-boolean"),
        pytest.param(httpx.Response(200, json={"authorized": "false"}), id="string-boolean"),
        pytest.param(httpx.Response(200, json={"authorized": True}), id="missing-code"),
        pytest.param(
            httpx.Response(200, json={"authorized": True, "authorization_code": " \t"}),
            id="blank-code",
        ),
        pytest.param(
            httpx.Response(200, json={"authorized": True, "authorization_code": 123}),
            id="non-string-code",
        ),
        pytest.param(httpx.Response(200, content=b"not JSON"), id="malformed-json"),
        pytest.param(
            httpx.Response(200, content=rb'{"authorized":true,"authorization_code":"A\uD800B"}'),
            id="unpaired-surrogate-code",
        ),
        pytest.param(
            httpx.Response(201, json={"authorized": True, "authorization_code": "code"}),
            id="unexpected-status",
        ),
    ],
)
async def test_unconfirmed_response_is_unknown_without_retry_or_storage(
    payment, repository, response
):
    calls = []

    def handler(request):
        calls.append(request)
        return response

    async with httpx.AsyncClient(
        base_url="http://bank.test", transport=httpx.MockTransport(handler)
    ) as client:
        service = PaymentService(BankClient(client), repository, lambda: NOW)
        with pytest.raises(BankOutcomeUnknown) as error:
            await service.create(payment)

    assert error.value.status_code == 502
    assert error.value.error_code == "bank_outcome_unknown"
    assert len(calls) == 1
    assert len(repository) == 0


async def test_bank_redirect_is_not_followed(payment, repository):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": "http://other.test/payments"})

    async with httpx.AsyncClient(
        base_url="http://bank.test",
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        service = PaymentService(BankClient(client), repository, lambda: NOW)
        with pytest.raises(BankOutcomeUnknown):
            await service.create(payment)

    assert len(calls) == 1
    assert calls[0].url.host == "bank.test"
    assert len(repository) == 0


async def test_empty_simulator_503_is_unavailable_without_retry_or_storage(payment, repository):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, content=b"")

    async with httpx.AsyncClient(
        base_url="http://bank.test", transport=httpx.MockTransport(handler)
    ) as client:
        service = PaymentService(BankClient(client), repository, lambda: NOW)
        with pytest.raises(BankUnavailable) as error:
            await service.create(payment)

    assert error.value.status_code == 503
    assert error.value.error_code == "bank_unavailable"
    assert len(calls) == 1
    assert len(repository) == 0


@pytest.mark.parametrize(
    ("exception_type", "status", "code", "outcome"),
    [
        (httpx.ConnectError, 503, "bank_unavailable", "unavailable"),
        (httpx.ConnectTimeout, 503, "bank_unavailable", "unavailable"),
        (httpx.PoolTimeout, 503, "bank_unavailable", "unavailable"),
        (httpx.ReadTimeout, 504, "bank_outcome_unknown", "timeout"),
        (httpx.WriteError, 502, "bank_outcome_unknown", "unknown"),
        (httpx.RemoteProtocolError, 502, "bank_outcome_unknown", "unknown"),
    ],
)
async def test_transport_failure_certainty_is_preserved(
    payment, repository, exception_type, status, code, outcome
):
    calls = []
    observations = []

    def handler(request):
        calls.append(request)
        raise exception_type(f"Sensitive bank error: {PAN}, cvv={CVV}", request=request)

    async with httpx.AsyncClient(
        base_url="http://bank.test", transport=httpx.MockTransport(handler)
    ) as client:
        bank = BankClient(client, observe=lambda *event: observations.append(event))
        service = PaymentService(bank, repository, lambda: NOW)
        with pytest.raises((BankUnavailable, BankOutcomeUnknown)) as error:
            await service.create(payment)

    assert error.value.status_code == status
    assert error.value.error_code == code
    assert PAN not in str(error.value)
    assert CVV not in str(error.value)
    assert len(calls) == 1
    assert len(repository) == 0
    assert [event[0] for event in observations] == [outcome]


@pytest.mark.parametrize("caller_cancels", [False, True], ids=["deadline", "caller"])
async def test_pending_submission_is_cancelled_once_without_storage(
    payment, repository, caller_cancels
):
    calls = []
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(request):
        calls.append(request)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with httpx.AsyncClient(
        base_url="http://bank.test", transport=httpx.MockTransport(handler)
    ) as client:
        bank = BankClient(client, deadline_seconds=1 if caller_cancels else 0.02)
        service = PaymentService(bank, repository, lambda: NOW)
        async with asyncio.timeout(2):
            task = asyncio.create_task(service.create(payment))
            await started.wait()
            if caller_cancels:
                task.cancel()
            with pytest.raises(
                asyncio.CancelledError if caller_cancels else BankOutcomeUnknown
            ) as error:
                await task

    if not caller_cancels:
        assert error.value.status_code == 504
        assert error.value.error_code == "bank_outcome_unknown"
    assert cancelled.is_set()
    assert len(calls) == 1
    assert len(repository) == 0


async def test_telemetry_failure_does_not_lose_authorization(payment, repository):
    def observer(_outcome, _duration):
        raise RuntimeError("Telemetry failed")

    async with httpx.AsyncClient(
        base_url="http://bank.test",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"authorized": True, "authorization_code": "code"})
        ),
    ) as client:
        service = PaymentService(BankClient(client, observe=observer), repository, lambda: NOW)
        result = await service.create(payment)

    assert result.status == "Authorized"
    assert result.authorization_code == "code"
    assert repository.get(result.id) is result
