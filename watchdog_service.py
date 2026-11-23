"""Deprecated watchdog module.

The only active reservation scheduler is watchdog_worker in rep_bot.py.
The previous class referenced retired database/X-UI APIs and must not be started.
The account early-warning monitor has not been implemented and is intentionally
not reported as an available feature.
"""


class WatchdogService:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "WatchdogService is retired. Use the canonical watchdog_worker "
            "registered by rep_bot.main()."
        )
