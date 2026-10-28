import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from payment_gateway_api.app import create_app
from payment_gateway_api.bank import BankDecision
from payment_gateway_api.config import Settings
from payment_gateway_api.errors import BankOutcomeUnknown, BankUnavailable

PAN = "01234567890001"
NOW = datetime(2026, 10, 28, tzinfo=UTC)
REQUEST = {
    "card_number": PAN,
    "expiry_month": 12,
    "expiry_year": 2030,
    "currency": "GBP",
    "amount": 1,
    "cvv": "012",
}


class StubBank:
    def __init__(self):
        self.decision = BankDecision(True, "opaque-bank-reference")
        self.failure = None
        self.calls = []

    async def submit(self, payment):
        self.calls.append(payment)
        if self.failure is not None:
            raise self.failure
        return self.decision


@pytest.fixture
def api():
    bank = StubBank()
    app = create_app(bank=bank, clock=lambda: NOW)
    with TestClient(app) as client:
        yield client, bank


@pytest.mark.parametrize("authorized", [True, False])
def test_confirmed_payments_are_masked_and_retrievable(api, authorized):
    client, bank = api
    bank.decision = BankDecision(authorized, "bank-reference" if authorized else None)
    payload = (
        REQUEST
        if authorized
        else REQUEST
        | {
            "card_number": "0000000000000000001",
            "cvv": "0001",
            "currency": "EUR",
            "expiry_month": 1,
            "amount": 1050,
        }
    )

    created = client.post("/payments", json=payload)

    assert created.status_code == 201
    payment = created.json()
    assert str(UUID(payment["id"])) == payment["id"]
    assert payment == {
        "id": payment["id"],
        "status": "Authorized" if authorized else "Declined",
        "card_number_last_four": payload["card_number"][-4:],
        "expiry_month": payload["expiry_month"],
        "expiry_year": payload["expiry_year"],
        "currency": payload["currency"],
        "amount": payload["amount"],
        "authorization_code": "bank-reference" if authorized else None,
    }
    assert created.headers["Location"] == f"/payments/{payment['id']}"
    retrieved = client.get(created.headers["Location"])
    assert retrieved.status_code == 200
    assert retrieved.json() == payment
    assert len(bank.calls) == len(client.app.state.repository) == 1
    assert bank.calls[0].cvv == payload["cvv"]
    assert payload["card_number"] not in repr(bank.calls[0])
    assert "cvv=" not in repr(bank.calls[0])
    for response in (created, retrieved):
        assert response.headers["Cache-Control"] == "no-store"
        assert payload["card_number"] not in response.text
        assert "cvv" not in response.json()


@pytest.mark.parametrize(
    "change",
    [
        {"card_number": "1" * 13},
        {"card_number": "1" * 20},
        {"card_number": int(PAN)},
        {"card_number": " " + PAN},
        {"card_number": PAN + "\n"},
        {"card_number": "١" * 14},
        {"cvv": "12"},
        {"cvv": "12345"},
        {"cvv": 123},
        {"cvv": "123\n"},
        {"cvv": "１２３"},
        {"cvv": None},
        {"amount": True},
        {"amount": 1.0},
        {"amount": "1"},
        {"amount": 0},
        {"amount": -1},
        {"expiry_month": 0},
        {"expiry_month": 13},
        {"expiry_year": 10000},
        {"currency": "usd"},
        {"currency": "JPY"},
        {PAN: "extra"},
    ],
)
def test_invalid_fields_are_rejected_without_bank_calls_or_storage(api, change):
    client, bank = api
    response = client.post("/payments", json=REQUEST | change)
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "Rejected"
    assert body["error_code"] == "invalid_payment"
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert PAN not in response.text
    assert body["errors"]
    assert all(error["field"] in {*REQUEST, "body"} for error in body["errors"])
    assert all(set(error) == {"field", "message"} for error in body["errors"])
    assert bank.calls == []
    assert len(client.app.state.repository) == 0
    if change == {"card_number": "1" * 13}:
        assert body["errors"][0]["message"] == "Use 14–19 ASCII digits."
    if change == {"cvv": "12"}:
        assert body["errors"][0]["message"] == "Use 3–4 ASCII digits."


def test_all_required_fields_are_reported_together(api):
    client, bank = api
    response = client.post("/payments", json={})
    assert response.status_code == 422
    assert {error["field"] for error in response.json()["errors"]} == set(REQUEST)
    assert bank.calls == []
    assert len(client.app.state.repository) == 0


@pytest.mark.parametrize(
    ("content", "content_type", "status"),
    [
        (f'{{"card_number":"{PAN}",', "application/json", 400),
        (PAN, "text/plain", 400),
        (PAN, None, 400),
        ("", "application/json", 400),
        ("null", "application/json", 422),
    ],
)
def test_invalid_body_shapes_use_sanitized_rejection(api, content, content_type, status):
    client, bank = api
    headers = {} if content_type is None else {"Content-Type": content_type}
    response = client.post("/payments", content=content, headers=headers)
    assert response.status_code == status
    assert response.json()["status"] == "Rejected"
    assert PAN not in response.text
    assert bank.calls == []
    assert len(client.app.state.repository) == 0


def test_non_finite_json_is_rejected_before_duplicate_fields_hide_it(api):
    client, bank = api
    for value in ("NaN", "Infinity", "-Infinity"):
        body = json.dumps(REQUEST)[:-1] + f',"amount":{value},"amount":100}}'
        response = client.post(
            "/payments", content=body, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "invalid_json"
        assert response.json()["status"] == "Rejected"
        assert PAN not in response.text
    assert bank.calls == []
    assert len(client.app.state.repository) == 0


@pytest.mark.parametrize(
    ("now", "year", "month", "status"),
    [
        (NOW, 2026, 9, 422),
        (NOW, 2026, 11, 201),
        (datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC), 2026, 12, 201),
        (datetime(2027, 1, 1, tzinfo=UTC), 2026, 12, 422),
        (datetime(2026, 11, 1, 0, 30, tzinfo=timezone(timedelta(hours=1))), 2026, 10, 201),
    ],
)
def test_expiry_uses_month_end_in_utc(now, year, month, status):
    bank = StubBank()
    with TestClient(create_app(bank=bank, clock=lambda: now)) as client:
        response = client.post(
            "/payments", json=REQUEST | {"expiry_year": year, "expiry_month": month}
        )
        assert response.status_code == status
        assert len(bank.calls) == len(client.app.state.repository) == int(status == 201)


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (BankUnavailable(), 503, "bank_unavailable"),
        (BankOutcomeUnknown(502), 502, "bank_outcome_unknown"),
        (BankOutcomeUnknown(504), 504, "bank_outcome_unknown"),
    ],
)
def test_bank_errors_keep_their_meaning_and_do_not_create_payments(
    api, caplog, failure, status, code
):
    client, bank = api
    bank.failure = failure
    with caplog.at_level(logging.INFO, logger="payment_gateway"):
        response = client.post("/payments", json=REQUEST)
    assert response.status_code == status
    assert response.json()["error_code"] == code
    assert "status" not in response.json()
    assert "Retry-After" not in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    assert len(bank.calls) == 1
    assert len(client.app.state.repository) == 0
    failures = [record for record in caplog.records if record.getMessage() == "payment_failed"]
    assert len(failures) == 1
    assert failures[0].request_id == response.headers["X-Request-ID"]
    assert failures[0].exc_info is None


def test_historical_get_and_liveness_work_after_expiry_and_bank_failure():
    now = [NOW]
    bank = StubBank()
    with TestClient(create_app(bank=bank, clock=lambda: now[0])) as client:
        created = client.post("/payments", json=REQUEST | {"expiry_year": 2026, "expiry_month": 10})
        assert created.status_code == 201
        bank.failure = BankUnavailable()
        assert client.post("/payments", json=REQUEST).status_code == 503
        now[0] = datetime(2031, 1, 1, tzinfo=UTC)
        assert client.get(created.headers["Location"]).json() == created.json()
        assert client.get("/").json() == {"app": "payment-gateway-api"}
        assert len(bank.calls) == 2


def test_metrics_distinguish_payment_outcomes_and_bound_untrusted_labels(api):
    client, bank = api
    assert client.post("/payments", json=REQUEST).status_code == 201
    bank.decision = BankDecision(False, None)
    assert client.post("/payments", json=REQUEST).status_code == 201
    assert client.post("/payments", json=REQUEST | {"amount": False}).status_code == 422
    bank.failure = BankOutcomeUnknown(504)
    assert client.post("/payments", json=REQUEST).status_code == 504
    assert client.get(f"/payments/{PAN}").status_code == 404
    assert client.get(f"/unknown/{PAN}").status_code == 404
    client.app.state.metrics.observe_bank(PAN, 0.01)

    metrics = client.get("/metrics").text

    for status in ("Authorized", "Declined", "Rejected"):
        assert f'gateway_payment_outcomes_total{{status="{status}"}} 1.0' in metrics
    assert PAN not in metrics
    assert 'route="/payments/{payment_id}"' in metrics
    assert 'route="unmatched"' in metrics
    assert 'gateway_bank_request_duration_seconds_count{outcome="error"} 1.0' in metrics


def test_each_app_has_fresh_storage_client_and_metrics():
    first = create_app(bank=StubBank(), clock=lambda: NOW)
    second = create_app(bank=StubBank(), clock=lambda: NOW)
    with TestClient(first) as client:
        created = client.post("/payments", json=REQUEST)
        first_client = first.state.client
        assert not first_client.is_closed
    assert first_client.is_closed
    with TestClient(second) as client:
        missing = client.get(created.headers["Location"])
        assert missing.status_code == 404
        assert missing.json()["error_code"] == "payment_not_found"
        assert second.state.client is not first_client
        assert second.state.metrics.registry is not first.state.metrics.registry
        assert 'status="Authorized"' not in client.get("/metrics").text


def test_method_error_preserves_allow_header(api):
    client, _ = api
    response = client.delete("/payments")
    assert response.status_code == 405
    assert "POST" in response.headers["Allow"]
    assert response.headers["Cache-Control"] == "no-store"


def test_openapi_documents_the_safe_error_contract(api):
    client, _ = api
    schema = client.get("/openapi.json").json()
    post = schema["paths"]["/payments"]["post"]["responses"]
    assert post["422"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/RejectedResponse"
    )
    get = schema["paths"]["/payments/{payment_id}"]["get"]["responses"]
    assert "422" not in get
    assert "HTTPValidationError" not in schema["components"]["schemas"]


def test_documentation_routes_have_bounded_metrics(api):
    client, _ = api
    routes = ("/docs", "/openapi.json", "/redoc", "/docs/oauth2-redirect")
    for route in routes:
        assert client.get(route).status_code == 200
    assert client.get(f"/docs/{PAN}").status_code == 404

    metrics = client.get("/metrics").text

    for route in routes:
        assert f'gateway_http_requests_total{{route="{route}",status="200"}} 1.0' in metrics
    assert 'gateway_http_requests_total{route="unmatched",status="404"} 1.0' in metrics
    assert PAN not in metrics


def test_environment_configuration_is_used_at_startup(monkeypatch):
    monkeypatch.setenv("BANK_URL", "http://bank_simulator:8080")
    monkeypatch.setenv("BANK_DEADLINE", "1.5")
    monkeypatch.setenv("LOG_LEVEL", "warning")
    app = create_app(bank=StubBank())
    with TestClient(app):
        assert app.state.settings.bank_url == "http://bank_simulator:8080"
        assert app.state.settings.bank_deadline == 1.5
        assert app.state.settings.log_level == "WARNING"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BANK_DEADLINE", "0"),
        ("BANK_DEADLINE", "nan"),
        ("BANK_DEADLINE", "not-a-number"),
        ("BANK_URL", "http://user:secret@bank"),
        ("LOG_LEVEL", "DEBUG"),
    ],
)
def test_invalid_configuration_fails_without_exposing_credentials(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as error:
        Settings.from_env()
    assert "secret" not in str(error.value)
