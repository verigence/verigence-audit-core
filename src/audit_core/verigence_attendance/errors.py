from __future__ import annotations


class AttendanceRuleError(RuntimeError):
    def __init__(self, code: str, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


class AttendanceNotFoundError(AttendanceRuleError):
    def __init__(self, detail: str) -> None:
        super().__init__("ATTENDANCE_NOT_FOUND", detail, status_code=404)
