from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Currency = Literal["GBP", "USD", "EUR"]
PaymentStatus = Literal["Authorized", "Declined"]


class PaymentRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    card_number: str = Field(pattern=r"^[0-9]{14,19}$", repr=False)
    expiry_month: int = Field(ge=1, le=12)
    expiry_year: int = Field(ge=1, le=9999)
    currency: Currency
    amount: int = Field(gt=0)
    cvv: str = Field(pattern=r"^[0-9]{3,4}$", repr=False)


class Payment(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    id: str
    status: PaymentStatus
    card_number_last_four: str = Field(pattern=r"^[0-9]{4}$")
    expiry_month: int = Field(ge=1, le=12)
    expiry_year: int = Field(ge=1, le=9999)
    currency: Currency
    amount: int = Field(gt=0)
    authorization_code: str | None

    @model_validator(mode="after")
    def validate_authorization_code(self) -> Self:
        if self.status == "Authorized" and (
            self.authorization_code is None or not self.authorization_code.strip()
        ):
            raise ValueError("Authorized payments require a bank authorization code.")
        if self.status == "Declined" and self.authorization_code is not None:
            raise ValueError("Declined payments have no authorization code.")
        return self
