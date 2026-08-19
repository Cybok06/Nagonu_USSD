"""USSD gateway adapters.

Provider-specific callback fields belong in this package.  Menu and order
modules should consume only the normalized request/response types.
"""

from .base import NormalizedUSSDRequest, NormalizedUSSDResponse, normalize_menu_response

__all__ = ["NormalizedUSSDRequest", "NormalizedUSSDResponse", "normalize_menu_response"]
