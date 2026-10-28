import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from uuid import UUID, uuid4


class SmokeFailure(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def exchange(base_url: str, method: str, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base_url + path,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method=method,
    )
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    except (OSError, urllib.error.URLError) as exc:
        raise SmokeFailure(f"Cannot reach the API for {method} {path}") from exc
    with response:
        try:
            payload = json.load(response)
        except (ValueError, UnicodeError) as exc:
            raise SmokeFailure(f"Non-JSON response for {method} {path}") from exc
        require(isinstance(payload, dict), "Expected a JSON object response")
        return response.status, response.headers, payload


def wait_for_api(base_url: str, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            status, _, _ = exchange(base_url, "GET", "/")
            if status == 200:
                return
        except SmokeFailure:
            pass
        if time.monotonic() >= deadline:
            raise SmokeFailure("API liveness did not become ready")
        time.sleep(0.25)


def safe_response(headers, body: dict, card_number: str) -> None:
    require(headers.get("Cache-Control") == "no-store", "Missing no-store header")
    try:
        UUID(headers.get("X-Request-ID", ""))
    except ValueError as exc:
        raise SmokeFailure("Missing or invalid request ID") from exc
    require(card_number not in json.dumps(body), "Response contains the full test PAN")
    require("cvv" not in body, "Response contains a CVV field")


def run(base_url: str, wait_seconds: float) -> None:
    wait_for_api(base_url, wait_seconds)
    template = {
        "card_number": "2222405343248877",
        "expiry_month": 12,
        "expiry_year": datetime.now(UTC).year + 2,
        "currency": "GBP",
        "amount": 1050,
        "cvv": "012",
    }
    saved: list[dict] = []
    expected_fields = {
        "id",
        "status",
        "card_number_last_four",
        "expiry_month",
        "expiry_year",
        "currency",
        "amount",
        "authorization_code",
    }
    for ending, expected_status in (("7", "Authorized"), ("8", "Declined")):
        payment = {**template, "card_number": template["card_number"][:-1] + ending}
        status, headers, body = exchange(base_url, "POST", "/payments", payment)
        require(status == 201, f"{expected_status} payment returned HTTP {status}")
        safe_response(headers, body, payment["card_number"])
        require(set(body) == expected_fields, "Payment response fields differ from the contract")
        require(body["status"] == expected_status, "Incorrect bank outcome")
        require(body["card_number_last_four"] == payment["card_number"][-4:], "Wrong last four")
        for field in ("expiry_month", "expiry_year", "currency", "amount"):
            require(body[field] == payment[field], f"Incorrect returned {field}")
        try:
            UUID(body["id"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise SmokeFailure("Payment ID is not a UUID string") from exc
        require(
            headers.get("Location", "").endswith(f"/payments/{body['id']}"),
            "Missing or incorrect Location header",
        )
        code = body["authorization_code"]
        if expected_status == "Authorized":
            require(isinstance(code, str) and bool(code.strip()), "Missing bank authorization code")
        else:
            require(code is None, "Declined payment code must be null")
        saved.append(body)

    status, headers, body = exchange(base_url, "POST", "/payments", {**template, "amount": 0})
    require(status == 422 and body.get("status") == "Rejected", "Invalid payment was not rejected")
    safe_response(headers, body, template["card_number"])

    unavailable = {**template, "card_number": template["card_number"][:-1] + "0"}
    status, headers, body = exchange(base_url, "POST", "/payments", unavailable)
    require(status == 503, "Simulator unavailable case did not return 503")
    require(body.get("error_code") == "bank_unavailable", "Incorrect unavailable error code")
    require("status" not in body and "id" not in body, "Bank failure invented a payment outcome")
    safe_response(headers, body, unavailable["card_number"])

    for original in saved:
        status, headers, body = exchange(base_url, "GET", f"/payments/{original['id']}")
        require(status == 200 and body == original, "GET did not reproduce the stored payment")
        safe_response(headers, body, template["card_number"])
    require(saved[0]["id"] != saved[1]["id"], "Confirmed payments reused an ID")

    status, _, _ = exchange(base_url, "GET", f"/payments/{uuid4()}")
    require(status == 404, "Unknown payment did not return 404")
    print("Smoke passed: Authorized, Declined, Rejected, bank unavailable, matching GETs and 404.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the payment gateway simulator checks.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--wait-seconds", type=float, default=30)
    args = parser.parse_args()
    try:
        run(args.base_url.rstrip("/"), args.wait_seconds)
    except SmokeFailure as exc:
        print(f"Smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
