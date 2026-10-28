import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx

from payment_gateway_api.errors import PaymentProcessingError
from tests.runtime_app import CVV_SENTINEL, PAN_SENTINEL

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_COMMAND = (
    "import sys; from main import run_server; "
    "run_server('tests.runtime_app:create_app', "
    "host='127.0.0.1', port=int(sys.argv[1]), factory=True)"
)
PUBLIC_ERROR_FIELDS = {"error_code", "message", "request_id"}
REQUEST_FIELDS = {
    "body",
    "card_number",
    "expiry_month",
    "expiry_year",
    "currency",
    "amount",
    "cvv",
}


@dataclass
class RuntimeServer:
    client: httpx.Client
    stdout_path: Path
    stderr_path: Path

    @property
    def output(self) -> str:
        return self.stdout_path.read_text() + self.stderr_path.read_text()

    def records(self) -> list[dict[str, object]]:
        return [
            json.loads(line) for line in self.stdout_path.read_text().splitlines() if line.strip()
        ]


@contextmanager
def running_server(tmp_path: Path) -> Iterator[RuntimeServer]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    stdout_path = tmp_path / "uvicorn.stdout.log"
    stderr_path = tmp_path / "uvicorn.stderr.log"
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "LOG_LEVEL": "INFO"}
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-c", SERVER_COMMAND, str(port)],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=stdout,
            stderr=stderr,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                timeout=2,
                trust_env=False,
            ) as client:
                server = RuntimeServer(client, stdout_path, stderr_path)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise AssertionError(f"Uvicorn exited during startup:\n{server.output}")
                    try:
                        if client.get("/").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    raise AssertionError(f"Uvicorn did not become ready:\n{server.output}")
                yield server
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def payment_body() -> dict[str, object]:
    return {
        "card_number": PAN_SENTINEL,
        "expiry_month": 12,
        "expiry_year": 2031,
        "currency": "GBP",
        "amount": 1050,
        "cvv": CVV_SENTINEL,
    }


def assert_private(text: str) -> None:
    assert PAN_SENTINEL not in text
    assert not re.search(rf"[\"']?\bcvv[\"']?\s*[:=]\s*[\"']?{CVV_SENTINEL}\b", text, re.IGNORECASE)


def assert_safe_error(response: httpx.Response, status_code: int) -> str:
    assert response.status_code == status_code
    assert response.headers["cache-control"] == "no-store"
    request_id = response.headers["x-request-id"]
    assert str(UUID(request_id)) == request_id
    assert request_id != PAN_SENTINEL
    assert response.json()["request_id"] == request_id
    assert_private(response.text)
    assert_private(json.dumps(dict(response.headers)))
    assert "location" not in response.headers
    return request_id


def assert_safe_logs(server: RuntimeServer) -> list[dict[str, object]]:
    assert_private(server.output)
    assert "Exception in ASGI application" not in server.output
    assert "Traceback (most recent call last)" not in server.output
    records = server.records()
    assert records, "The production application logger must emit structured stdout events"
    for record in records:
        assert not {"card_number", "cvv", "body", "headers", "exc_info"} & record.keys()
    return records


def assert_completion(
    records: list[dict[str, object]],
    request_id: str,
    status_code: int,
    route: str,
) -> None:
    events = [
        record
        for record in records
        if record.get("request_id") == request_id and record.get("event") == "request_completed"
    ]
    assert len(events) == 1
    assert events[0]["status_code"] == status_code
    assert events[0]["route"] == route


def assert_one_safe_failure(records: list[dict[str, object]], request_id: str) -> None:
    events = [
        record
        for record in records
        if record.get("request_id") == request_id and record.get("event") == "payment_failed"
    ]
    assert len(events) == 1
    assert events[0]["error_code"] == "payment_processing_error"


def test_real_uvicorn_contains_post_authorization_persistence_failure(tmp_path: Path) -> None:
    with running_server(tmp_path) as server:
        response = server.client.post(
            "/payments",
            json=payment_body(),
            headers={"X-Request-ID": PAN_SENTINEL},
        )
        request_id = assert_safe_error(response, 500)
        assert response.json() == {
            "error_code": "payment_processing_error",
            "message": PaymentProcessingError.message,
            "request_id": request_id,
        }
        assert server.client.get("/__test__/state").json() == {
            "call_order": ["bank_authorized", "save_failed"],
            "stored_count": 0,
        }
        assert server.client.get("/").status_code == 200

    records = assert_safe_logs(server)
    assert_one_safe_failure(records, request_id)
    assert_completion(records, request_id, 500, "/payments")


def test_real_uvicorn_contains_response_validation_failure(tmp_path: Path) -> None:
    with running_server(tmp_path) as server:
        response = server.client.get("/__test__/response-validation")
        request_id = assert_safe_error(response, 500)
        assert response.json() == {
            "error_code": "payment_processing_error",
            "message": PaymentProcessingError.message,
            "request_id": request_id,
        }
        assert server.client.get("/__test__/state").json() == {
            "call_order": [],
            "stored_count": 0,
        }
        assert server.client.get("/").status_code == 200

    records = assert_safe_logs(server)
    assert_one_safe_failure(records, request_id)
    assert_completion(records, request_id, 500, "unmatched")


def test_real_uvicorn_does_not_log_rejected_input_or_untrusted_metadata(tmp_path: Path) -> None:
    invalid_bodies = [
        (f'{{"card_number":"{PAN_SENTINEL}","cvv":"{CVV_SENTINEL}",}}', "application/json", 400),
        (f"card_number={PAN_SENTINEL}&cvv={CVV_SENTINEL}", "text/plain", 400),
        (json.dumps({"card_number": PAN_SENTINEL, "cvv": CVV_SENTINEL}), "application/json", 422),
        (json.dumps({**payment_body(), PAN_SENTINEL: CVV_SENTINEL}), "application/json", 422),
        (json.dumps({**payment_body(), "amount": PAN_SENTINEL}), "application/json", 422),
    ]
    completions: list[tuple[str, int, str]] = []
    with running_server(tmp_path) as server:
        for body, content_type, status_code in invalid_bodies:
            response = server.client.post(
                "/payments",
                content=body,
                headers={
                    "Content-Type": content_type,
                    "X-Request-ID": PAN_SENTINEL,
                    "Authorization": f"Bearer {PAN_SENTINEL}",
                },
            )
            request_id = assert_safe_error(response, status_code)
            rejected = response.json()
            assert set(rejected) == PUBLIC_ERROR_FIELDS | {"status", "errors"}
            assert rejected["status"] == "Rejected"
            assert rejected["errors"]
            for error in rejected["errors"]:
                assert set(error) == {"field", "message"}
                assert error["field"] in REQUEST_FIELDS
            completions.append((request_id, status_code, "/payments"))

        for path, route in (
            (f"/payments/{PAN_SENTINEL}", "/payments/{payment_id}"),
            (f"/payments/{PAN_SENTINEL}/", "unmatched"),
            (f"/unknown/{PAN_SENTINEL}", "unmatched"),
        ):
            response = server.client.get(
                path,
                params={"card_number": PAN_SENTINEL, "cvv": CVV_SENTINEL},
                headers={"X-Request-ID": PAN_SENTINEL},
            )
            request_id = assert_safe_error(response, 404)
            completions.append((request_id, 404, route))

        assert server.client.get("/__test__/state").json() == {
            "call_order": [],
            "stored_count": 0,
        }

    records = assert_safe_logs(server)
    assert len({request_id for request_id, _, _ in completions}) == len(completions)
    for request_id, status_code, route in completions:
        assert_completion(records, request_id, status_code, route)
