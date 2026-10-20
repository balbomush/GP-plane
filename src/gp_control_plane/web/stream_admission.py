"""Compatibility alias for Core-owned gp_control_plane.http.stream_admission."""
import sys
from ..http import stream_admission as _implementation
sys.modules[__name__] = _implementation
