"""Compatibility alias for Core-owned gp_control_plane.http.api_server."""
import sys
from ..http import api_server as _implementation
sys.modules[__name__] = _implementation
