from __future__ import annotations


class BackupCLIError(RuntimeError):
    """Error controlado de la aplicación."""


class AuthenticationDownloadError(BackupCLIError):
    """La sesión fue rechazada durante la resolución o descarga."""


class ResourceAccessDeniedError(BackupCLIError):
    """The resource was rejected while the global session remains valid."""


class GlobalAuthenticationAbort(BackupCLIError):
    """A global authentication failure requires stopping the run."""


class TransientDownloadError(BackupCLIError):
    """Error temporal que permite reintentar sin perder el archivo parcial."""

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after
