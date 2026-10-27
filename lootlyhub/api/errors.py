class LootlyError(Exception):
    """Ошибка Seller API: {"success": false, "error": {"code": "..."}}."""

    def __init__(self, code: str, status: int, path: str = ""):
        self.code = code
        self.status = status
        self.path = path
        super().__init__(f"{code} (HTTP {status}) {path}".strip())

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500


class RateLimited(LootlyError):
    def __init__(self, retry_after: float, path: str = ""):
        super().__init__("RATE_LIMIT", 429, path)
        self.retry_after = retry_after
