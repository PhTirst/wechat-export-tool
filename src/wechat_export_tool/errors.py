import re


class ExportError(RuntimeError):
    """A fixed, non-secret diagnostic code suitable for CLI output."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code) else "operation_failed"
        super().__init__(self.code)
