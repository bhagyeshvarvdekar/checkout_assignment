class GatewayError(Exception):
    status_code = 500
    error_code = "payment_processing_error"
    message = "Payment processing failed; the payment outcome may be unknown."

    def __init__(self) -> None:
        super().__init__(self.message)


class BankUnavailable(GatewayError):
    status_code = 503
    error_code = "bank_unavailable"
    message = "The bank is unavailable. Consult the retry policy before resubmitting."


class BankOutcomeUnknown(GatewayError):
    error_code = "bank_outcome_unknown"
    message = "The bank outcome is unknown. Do not blindly retry this payment."

    def __init__(self, status_code: int = 502) -> None:
        if status_code not in (502, 504):
            raise ValueError("Unknown bank outcome must use 502 or 504")
        self.status_code = status_code
        super().__init__()


class PaymentProcessingError(GatewayError):
    pass


class PaymentNotFound(GatewayError):
    status_code = 404
    error_code = "payment_not_found"
    message = "Payment not found."


class PaymentRejected(GatewayError):
    status_code = 422
    error_code = "invalid_payment"
    message = "The payment request is invalid."

    def __init__(self, field_errors: list[dict[str, str]]) -> None:
        self.field_errors = field_errors
        super().__init__()
