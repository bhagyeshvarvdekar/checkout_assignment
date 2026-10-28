import uvicorn

from payment_gateway_api.config import Settings


def run_server(
    app: str = "payment_gateway_api.app:app",
    *,
    host: str = "0.0.0.0",
    port: int = 8000,
    factory: bool = False,
) -> None:
    uvicorn.run(
        app,
        host=host,
        port=port,
        factory=factory,
        workers=1,
        reload=False,
        access_log=False,
        log_level=Settings.from_env().log_level.lower(),
    )


if __name__ == "__main__":
    run_server()
