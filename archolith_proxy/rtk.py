"""DEPRECATED: Backward-compatibility shim for filter_adapter module.

This module is deprecated. All imports should use archolith_proxy.filter_adapter instead.
The rtk.py module was renamed to filter_adapter.py to align with the archolith_filter
package naming convention.

This shim will be removed in a future version.
"""

from __future__ import annotations

import warnings

# Trigger deprecation warning when this module is imported
warnings.warn(
    "archolith_proxy.rtk is deprecated; use archolith_proxy.filter_adapter instead",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export everything from filter_adapter
from archolith_proxy.filter_adapter import (  # noqa: F401
    filter_request_body,
    filter_single_tool_result,
    filter_tool_messages,
    is_available,
    shrink_tail_tool_results,
    shrink_tool_call_args,
)
