"""Conservative classification of 3X-UI mutation responses.

A response is SUCCESS or FAILURE only when its explicit signals agree.
Missing, malformed or contradictory signals remain UNKNOWN so callers retain
the operation lock and reconcile instead of replaying/refunding prematurely.

The one non-ambiguous UNKNOWN is an explicit ``dispatched: False`` marker: the
request never left the process, so no remote effect can exist and the outcome is
a definite FAILURE that must be refunded rather than locked.
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

    # A pre-dispatch rejection (missing binding, unbound operation, unsupported
    # capability, panel row unavailable) never reached the panel. Treating it as
    # UNKNOWN strands the customer's money in a lock for a service that was never
    # requested, so it is classified as a definite failure and refunded at once.
    if result.get("dispatched") is False:
        return XUIResult.FAILURE

    raw_status = result.get("status")
    status = raw_status.strip().lower() if isinstance(raw_status, str) else ""
    success_flag = result.get("success")
    if "success" in result and not isinstance(success_flag, bool):
        return XUIResult.UNKNOWN
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
