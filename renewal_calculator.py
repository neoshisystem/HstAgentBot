"""Canonical W06.4 renewal-calculator import surface.

The implementation is shared with the XUI adapter under services.
"""
from services.renewal_calculator import (
    RenewalCalculationError,
    calculate_addon,
    calculate_renewal,
)

__all__ = ["RenewalCalculationError", "calculate_renewal", "calculate_addon"]
