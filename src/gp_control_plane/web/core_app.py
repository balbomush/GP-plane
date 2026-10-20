"""Compatibility alias for Core-owned gp_control_plane.http.core_app."""
import sys
from ..http import core_app as _implementation
sys.modules[__name__] = _implementation
