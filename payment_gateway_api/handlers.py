import json
from typing import Literal, NoReturn

from fastapi import Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from payment_gateway_api.errors import GatewayError, PaymentProcessingError, PaymentRejected
from payment_gateway_api.models import PaymentRequest
from payment_gateway_api.observability import log_event, route_label

PAYMENT_FIELDS = frozenset(PaymentRequest.model_fields)


VALIDATION_MESSAGES = {
    "missing": "This field is required.",
    "extra_forbidden": "Unexpected field.",
    "int_type": "Use an integer.",
    "string_type": "Use a string.",
    "string_too_short": "Length is outside the allowed range.",
    "string_too_long": "Length is outside the allowed range.",
    "string_pattern_mismatch": "Use ASCII digits only.",
    "literal_error": "Choose a supported value.",
    "greater_than": "Value is outside the allowed range.",
    "greater_than_equal": "Value is outside the allowed range.",
    "less_than_equal": "Value is outside the allowed range.",
}


PATTERN_MESSAGES = {
    "card_number": "Use 14–19 ASCII digits.",
    "cvv": "Use 3–4 ASCII digits.",
}


class FieldError(BaseModel):
    field: str
    message: str


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    request_id: str


class RejectedResponse(ErrorResponse):
    status: Literal["Rejected"] = "Rejected"
    errors: list[FieldError]


def _json_media_type(request: Request) -> bool:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError("Non-finite JSON number")


async def require_json(request: Request) -> None:
    if not _json_media_type(request):
        raise HTTPException(status_code=400)
    try:
        json.loads(await request.body(), parse_constant=_reject_json_constant)
    except ValueError:
        raise HTTPException(status_code=400) from None


def _field_error(error: dict) -> dict[str, str]:
    location = error.get("loc", ())
    field = location[1] if len(location) > 1 and location[1] in PAYMENT_FIELDS else "body"
    message = VALIDATION_MESSAGES.get(error.get("type"), "Invalid value.")
    if error.get("type") == "string_pattern_mismatch":
        message = PATTERN_MESSAGES.get(field, message)
    return {"field": field, "message": message}


def error_response(
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    errors: list[dict[str, str]] | None = None,
) -> JSONResponse:
    request.state.error_code = error_code
    content = {
        "error_code": error_code,
        "message": message,
        "request_id": request.state.request_id,
    }
    if errors is not None:
        content.update(status="Rejected", errors=errors)
        if request.method == "POST" and route_label(request.scope) == "/payments":
            request.app.state.metrics.payments.labels(status="Rejected").inc()
    if status_code >= 500:
        log_event(
            "payment_failed",
            severity="ERROR",
            route=route_label(request.scope),
            status_code=status_code,
            error_code=error_code,
        )
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers={"X-Request-ID": request.state.request_id, "Cache-Control": "no-store"},
    )


async def gateway_error(request: Request, exc: GatewayError):
    field_errors = exc.field_errors if isinstance(exc, PaymentRejected) else None
    return error_response(request, exc.status_code, exc.error_code, exc.message, field_errors)


async def response_validation_error(request: Request, _exc: ResponseValidationError):
    return await gateway_error(request, PaymentProcessingError())


async def request_validation_error(request: Request, exc: RequestValidationError):
    malformed = (
        not _json_media_type(request)
        or not await request.body()
        or any(error.get("type") == "json_invalid" for error in exc.errors())
    )
    if malformed:
        return await http_error(request, HTTPException(status_code=400))
    return error_response(
        request,
        422,
        "invalid_payment",
        "The payment request is invalid.",
        [_field_error(error) for error in exc.errors()],
    )


async def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 400:
        return error_response(
            request,
            400,
            "invalid_json",
            "Supply a valid JSON object.",
            [{"field": "body", "message": "Supply a valid JSON object."}],
        )
    code = "not_found" if exc.status_code == 404 else "http_error"
    message = "Resource not found." if exc.status_code == 404 else "The request is not supported."
    response = error_response(request, exc.status_code, code, message)
    if exc.headers and "Allow" in exc.headers:
        response.headers["Allow"] = exc.headers["Allow"]
    return response


EXCEPTION_HANDLERS = {
    GatewayError: gateway_error,
    PaymentProcessingError: gateway_error,
    ResponseValidationError: response_validation_error,
    RequestValidationError: request_validation_error,
    HTTPException: http_error,
}
