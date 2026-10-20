"""Compatibility alias for Core-owned gp_control_plane.http.docs."""
import sys
from ..http import docs as _implementation
sys.modules[__name__] = _implementation
