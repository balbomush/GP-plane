"""Compatibility alias for Core-owned gp_control_plane.http.http_body."""
import sys
from ..http import http_body as _implementation
sys.modules[__name__] = _implementation
