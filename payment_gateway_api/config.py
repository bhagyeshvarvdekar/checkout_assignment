import math
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

TIMEOUT_FIELDS = (
    "bank_connect_timeout",
    "bank_read_timeout",
    "bank_write_timeout",
    "bank_pool_timeout",
    "bank_deadline",
)


@dataclass(frozen=True)
class Settings:
    bank_url: str = "http://localhost:8080"
    bank_connect_timeout: float = 1.0
    bank_read_timeout: float = 2.0
    bank_write_timeout: float = 2.0
    bank_pool_timeout: float = 1.0
    bank_deadline: float = 3.0
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        try:
            url = urlsplit(self.bank_url)
            valid_url = (
                url.scheme in {"http", "https"}
                and bool(url.hostname)
                and url.port != 0
                and url.username is None
                and url.password is None
                and url.path in {"", "/"}
                and not url.query
                and not url.fragment
                and not any(char.isspace() for char in self.bank_url)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("BANK_URL must be an HTTP(S) origin without credentials or a path")

        for name in TIMEOUT_FIELDS:
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name.upper()} must be a finite positive number")
        if self.log_level not in {"INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be INFO, WARNING, ERROR, or CRITICAL")

    @classmethod
    def from_env(cls) -> Settings:
        defaults = cls()
        values: dict[str, str | float] = {
            "bank_url": os.getenv("BANK_URL", defaults.bank_url),
            "log_level": os.getenv("LOG_LEVEL", defaults.log_level).upper(),
        }
        for name in TIMEOUT_FIELDS:
            try:
                values[name] = float(os.getenv(name.upper(), str(getattr(defaults, name))))
            except ValueError:
                raise ValueError(f"{name.upper()} must be a finite positive number") from None
        return cls(**values)
