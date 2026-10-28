# Payment gateway

FastAPI service for the Checkout.com challenge. It validates card payments, calls the supplied bank simulator once, and keeps confirmed results in memory for later retrieval.

## Run with Docker

Requires Docker Compose v2, a running Docker engine, and free ports 8000 and 8080. The smoke script needs Python 3.11 or later and uses only the standard library.

```sh
docker compose up --build -d --wait
python3 scripts/smoke.py
```

Open [Swagger UI](http://127.0.0.1:8000/docs) for the request and response schemas. `/` is a liveness check that doesn't call the bank; `/metrics` exposes Prometheus metrics. Published ports bind to loopback. The simulator's admin port is unpublished and its fixture is mounted read-only.

```sh
docker compose logs api
docker compose down
```

## Local development

Use Python 3.14.4, Poetry 2.2.1, and `make`. Dependencies are locked in `poetry.lock`.

```sh
poetry env use python3.14
make install
make check
```

`make check` runs Ruff and pytest. `make test` runs the tests without Docker. To run the API on the host, stop any Compose API using port 8000, then:

```sh
docker compose up -d bank_simulator
make run
```

Run `make smoke` in another terminal once the API is listening.

## API examples

Use synthetic cards only. This request authorizes; change the last card digit to `8` for a decline or `0` for the simulator's 503 case. Update the expiry if necessary.

```sh
curl -i http://127.0.0.1:8000/payments \
  -H 'Content-Type: application/json' \
  --data '{
    "card_number": "2222405343248877",
    "expiry_month": 12,
    "expiry_year": 2030,
    "currency": "GBP",
    "amount": 1050,
    "cvv": "012"
  }'
```

Authorized and Declined both return 201 with a `Location` header because both create a record. Copy the returned `id` to retrieve it:

```sh
PAYMENT_ID='<id from POST>'
curl -i "http://127.0.0.1:8000/payments/$PAYMENT_ID"
curl -i http://127.0.0.1:8000/payments/unknown
curl -i http://127.0.0.1:8000/payments \
  -H 'Content-Type: application/json' --data '{}'
```

These return 200, 404, and 422 respectively. POST and GET return the same fields: `id`, `status`, `card_number_last_four`, `expiry_month`, `expiry_year`, `currency`, `amount`, and `authorization_code`. The gateway ID is a UUID. The bank code is an opaque, nonblank string for Authorized and `null` for Declined. It is an informational addition to the brief and provides no deduplication guarantee.

## Validation

| Field | Rule |
| --- | --- |
| `card_number` | String of 14–19 ASCII digits |
| `expiry_month` | Integer, 1–12 |
| `expiry_year` | Integer, 1–9999; combined expiry must still be valid |
| `currency` | `GBP`, `USD`, or `EUR`, uppercase |
| `amount` | Positive integer in minor units; `1050` means GBP 10.50 |
| `cvv` | String of 3–4 ASCII digits |

All fields are required. Types are strict: no nulls, extra fields, boolean amounts, floats, or numeric-string coercion. Nonstandard JSON numbers such as `NaN` and `Infinity` are rejected, including when a duplicate field would hide them. Card number and CVV strings preserve leading zeroes; whitespace and separators are rejected. There is no Luhn or card-brand check.

Cards are valid through the end of their expiry month in UTC. This check applies only to creation, so GET still works after expiry. Month-end validity, positive amounts, and strict input handling are documented assumptions beyond the brief's minimum rules. Invalid input is Rejected without a bank call or stored record.

## Errors and retries

| Condition | HTTP / `error_code` |
| --- | --- |
| Malformed JSON or non-JSON body | 400 / `invalid_json` |
| Invalid payment fields | 422 / `invalid_payment` |
| Unknown or malformed payment ID | 404 / `payment_not_found` |
| Connection/pool failure before dispatch, or simulator 503 | 503 / `bank_unavailable` |
| Read/write timeout or overall deadline | 504 / `bank_outcome_unknown` |
| Other bank transport failure, unexpected status, or invalid response | 502 / `bank_outcome_unknown` |
| Unexpected local processing failure | 500 / `payment_processing_error` |

Errors contain `error_code`, `message`, and `request_id`. Validation errors also include `status: "Rejected"` and an `errors` list of safe field/message pairs. Submitted values aren't echoed. Bank failures don't invent a Declined or Rejected status. Payment and error responses use `Cache-Control: no-store` and a generated `X-Request-ID`; incoming IDs are ignored. Use paths exactly as shown: trailing-slash redirects are disabled.

The bank client disables retries, redirects, and environment proxies. Its connection and pool errors establish that this attempt wasn't sent. The simulator's 503 branch is also treated as unprocessed, an assumption specific to this fixture. Neither case resolves an earlier attempt with an unknown outcome.

Read/write timeouts, an overall deadline, malformed responses, crashes, or failure to save or deliver an authorization can leave the outcome unknown. Resubmitting can duplicate a payment. There is no idempotency support, `Retry-After`, or bank lookup endpoint to resolve uncertainty. Don't blindly retry these failures.

## Configuration

Export settings for `make run`; the app doesn't load `.env` automatically. Compose reads `.env` for timeout/log settings and sets its own bank URL. See `.env.example`. Invalid settings fail startup.

| Variable | Default |
| --- | --- |
| `BANK_URL` | `http://localhost:8080`; Compose uses `http://bank_simulator:8080` |
| `BANK_CONNECT_TIMEOUT` | `1` second |
| `BANK_READ_TIMEOUT` | `2` seconds |
| `BANK_WRITE_TIMEOUT` | `2` seconds |
| `BANK_POOL_TIMEOUT` | `1` second |
| `BANK_DEADLINE` | `3` seconds overall |
| `LOG_LEVEL` | `INFO`; also accepts `WARNING`, `ERROR`, `CRITICAL` |

Phase timeouts bound individual waits. `BANK_DEADLINE` bounds the complete bank call.

## Design and limits

`app.py` creates the application and manages its HTTP client's lifetime. `routes.py` contains the endpoints; `handlers.py` handles HTTP validation and safe error responses. Request logging and metrics live in `observability.py`.

`PaymentService` applies the expiry rule and saves confirmed results. `BankClient` sends the five bank fields, formatting expiry as `MM/YYYY`, and requires a boolean bank decision and a nonblank authorization code that can be encoded as UTF-8. The repository stores immutable public records. Tests can supply their own bank, repository, and clock.

Run one worker and one replica. Dictionary reads and inserts run synchronously on the same event loop. Restarting loses all payments, and separate processes would have separate histories. GET and liveness continue working during a bank outage.

Full card numbers and CVVs are transient inputs and aren't stored, returned, or logged. JSON logs use generated request IDs, route templates, status, and duration; Uvicorn access logs are disabled. Metrics count HTTP requests and payment outcomes, plus bank duration by outcome. Labels have fixed categories, and bank failures aren't counted as declines. These safeguards don't establish production payment compliance.

This local demo has no authentication or durable storage. Production work would need durable payment attempts, atomic idempotency handling, bank reconciliation, and merchant isolation.

## Testing

Tests cover validation, expiry boundaries, stored outcomes, the bank wire contract, and failure handling. They include deadline cancellation and real Uvicorn checks for sensitive data in responses and logs. The Docker stack and simulator smoke were checked locally on native arm64. GitHub Actions is configured to run the checks and build the runtime image.

## Package and sources

```sh
python3 scripts/package_submission.py
unzip dist/checkout-payment-gateway.zip -d /tmp/checkout-review
cd /tmp/checkout-review/checkout-payment-gateway
```

Use a fresh extraction directory, then follow the Docker commands above. Stop any existing demo using the same ports. The ZIP contains source and build files, excluding environments, caches, `.git`, and `.env`. Initial setup downloads dependencies and images.

Based on the [challenge brief (`5f1ce249`)](https://github.com/cko-recruitment/.github/blob/5f1ce249179665b1d8a97202acfc9990fcc4c779/profile/README.md) and [Python starter (`9ffcd486`)](https://github.com/cko-recruitment/payment-gateway-challenge-python/tree/9ffcd486c25ce5d51aad182c0e3c5d4b9d89523b). `.editorconfig` and `imposters/` are unchanged. The fixture authorizes odd final card digits, declines even nonzero digits, and returns 503 for zero. If the submission portal needs a repository URL, use your own repository. Don't open a pull request against `cko-recruitment`.
