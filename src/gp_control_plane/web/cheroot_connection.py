"""Compatibility alias for Core-owned gp_control_plane.http.cheroot_connection."""
import sys
from ..http import cheroot_connection as _implementation
sys.modules[__name__] = _implementation
