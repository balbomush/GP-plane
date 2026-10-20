"""Compatibility alias for Core-owned gp_control_plane.http.core_runtime."""
import sys
from ..http import core_runtime as _implementation
sys.modules[__name__] = _implementation
