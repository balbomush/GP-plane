"""Compatibility alias for Core-owned gp_control_plane.http.legacy_http."""
import sys
from ..http import legacy_http as _implementation
sys.modules[__name__] = _implementation
