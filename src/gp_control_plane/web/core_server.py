"""Compatibility alias for Core-owned gp_control_plane.http.core_server."""
import sys
from ..http import core_server as _implementation
sys.modules[__name__] = _implementation
