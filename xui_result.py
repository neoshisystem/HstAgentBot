"""Conservative classification of 3X-UI mutation responses.

A response is SUCCESS or FAILURE only when its explicit signals agree.
Missing, malformed or contradictory signals remain UNKNOWN so callers retain
the operation lock and reconcile instead of replaying/refunding prematurely.
"""

from enum import Enum


class XUIResult(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


_SUCCESS_STATUSES = {"success", "ok"}
_FAILURE_STATUSES = {"error", "failed", "failure"}


def classify_xui_result(result) -> XUIResult:
    """Classify an external response without guessing on incomplete/conflicting data."""
    if not isinstance(result, dict):
        return XUIResult.UNKNOWN

    raw_status = result.get("status")
    status = raw_status.strip().lower() if isinstance(raw_status, str) else ""
    success_flag = result.get("success")
    success_signal = success_flag if isinstance(success_flag, bool) else None

    status_signal = None
    if status in _SUCCESS_STATUSES:
        status_signal = True
    elif status in _FAILURE_STATUSES:
        status_signal = False

    signals = [signal for signal in (status_signal, success_signal) if signal is not None]
    if not signals:
        return XUIResult.UNKNOWN
    if any(signal != signals[0] for signal in signals[1:]):
        return XUIResult.UNKNOWN
    return XUIResult.SUCCESS if signals[0] else XUIResult.FAILURE
