"""Compatibility alias for Core-owned gp_control_plane.http.routes."""
import sys
from ..http import routes as _implementation
sys.modules[__name__] = _implementation
