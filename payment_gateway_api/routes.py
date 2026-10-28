from fastapi import APIRouter, Depends, Request, Response

from payment_gateway_api.handlers import ErrorResponse, RejectedResponse, require_json
from payment_gateway_api.models import Payment, PaymentRequest

router = APIRouter(redirect_slashes=False)


@router.get("/")
async def ping() -> dict[str, str]:
    return {"app": "payment-gateway-api"}


@router.post(
    "/payments",
    response_model=Payment,
    status_code=201,
    dependencies=[Depends(require_json)],
    responses={
        400: {"model": RejectedResponse},
        422: {"model": RejectedResponse},
        500: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
        504: {"model": ErrorResponse},
    },
)
async def create_payment(payment: PaymentRequest, request: Request, response: Response):
    result = await request.app.state.service.create(payment)
    request.state.payment_id = result.id
    request.app.state.metrics.payments.labels(status=result.status).inc()
    response.headers["Location"] = f"/payments/{result.id}"
    return result


@router.get(
    "/payments/{payment_id}",
    response_model=Payment,
    responses={
        404: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        "default": {"model": ErrorResponse},
    },
)
async def get_payment(payment_id: str, request: Request):
    result = request.app.state.service.get(payment_id)
    request.state.payment_id = result.id
    return result


@router.get("/metrics", include_in_schema=False)
async def get_metrics(request: Request):
    metrics = request.app.state.metrics
    return Response(content=metrics.render(), media_type=metrics.content_type)
