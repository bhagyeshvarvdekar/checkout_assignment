from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

from payment_gateway_api.bank import BankClient
from payment_gateway_api.config import Settings
from payment_gateway_api.handlers import EXCEPTION_HANDLERS
from payment_gateway_api.observability import Metrics, configure_logging, observe_request
from payment_gateway_api.repository import InMemoryPaymentRepository
from payment_gateway_api.routes import router
from payment_gateway_api.service import PaymentService


def create_app(
    *, settings: Settings | None = None, bank=None, repository=None, clock=None
) -> FastAPI:
    metrics = Metrics()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configuration = settings if settings is not None else Settings.from_env()
        configure_logging(configuration.log_level)
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
        transport = httpx.AsyncHTTPTransport(retries=0, limits=limits, trust_env=False)
        async with httpx.AsyncClient(
            base_url=configuration.bank_url,
            timeout=httpx.Timeout(
                connect=configuration.bank_connect_timeout,
                read=configuration.bank_read_timeout,
                write=configuration.bank_write_timeout,
                pool=configuration.bank_pool_timeout,
            ),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            payment_repository = (
                repository if repository is not None else InMemoryPaymentRepository()
            )
            bank_client = (
                bank
                if bank is not None
                else BankClient(
                    client,
                    deadline_seconds=configuration.bank_deadline,
                    observe=metrics.observe_bank,
                )
            )
            application.state.settings = configuration
            application.state.client = client
            application.state.repository = payment_repository
            application.state.service = PaymentService(
                bank_client,
                payment_repository,
                clock if clock is not None else lambda: datetime.now(UTC),
            )
            yield

    application = FastAPI(
        title="Payment Gateway",
        version="1.0.0",
        lifespan=lifespan,
        redirect_slashes=False,
        exception_handlers=EXCEPTION_HANDLERS,
    )
    application.state.metrics = metrics
    application.include_router(router)
    application.add_middleware(BaseHTTPMiddleware, dispatch=observe_request)
    return application


app = create_app()
