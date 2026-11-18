"""Bank provider exceptions."""


class BankProviderError(Exception):
    """Base exception for all bank provider operations."""

    def __init__(self, message: str, provider_name: str = "unknown"):
        super().__init__(message)
        self.message = message
        self.provider_name = provider_name


class BankAuthenticationError(BankProviderError):
    """Raised when bank provider authentication fails (e.g. invalid or expired credentials)."""

    pass


class BankRateLimitError(BankProviderError):
    """Raised when rate limits are exceeded (e.g. SimpleFIN 24 req/day limit)."""

    def __init__(
        self,
        message: str = "Provider rate limit reached (max requests per day exceeded)",
        provider_name: str = "unknown",
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message, provider_name=provider_name)
        self.retry_after_seconds = retry_after_seconds


class BankNetworkError(BankProviderError):
    """Raised when bank provider endpoint is unreachable or times out."""

    pass


class BankInvalidTokenError(BankProviderError):
    """Raised when setup token cannot be decoded or claimed."""

    pass
