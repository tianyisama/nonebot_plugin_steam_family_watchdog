class SteamError(Exception):
    def __init__(self, code: str, message: str, retry_after_seconds: int = 0, eresult: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.eresult = eresult
