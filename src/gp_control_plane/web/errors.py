"""Compatibility alias for Core-owned gp_control_plane.http.errors."""
import sys
from ..http import errors as _implementation
sys.modules[__name__] = _implementation
