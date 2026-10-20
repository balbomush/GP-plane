"""Compatibility alias for Core-owned gp_control_plane.http.http_runtime."""
import sys
from ..http import http_runtime as _implementation
sys.modules[__name__] = _implementation
