"""Safe application errors; never include credentials or raw request bodies."""


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: list[dict[str, str]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []
        self.headers = headers or {}


def invalid_field(field: str, message: str) -> ApiError:
    return ApiError(
        422,
        "VALIDATION_ERROR",
        "请求参数不符合要求",
        details=[{"field": field, "message": message}],
    )
