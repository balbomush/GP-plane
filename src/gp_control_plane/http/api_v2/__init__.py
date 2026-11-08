"""Core-owned API 2 endpoints."""

from .access import register_access

from .domain_lists import register_domain_lists

from .backup import register_backups
from .catalog import register_catalog

from .history import register_history
from .search_input import validate_start
from .service import register_service
from .sources import register_sources

__all__ = ["register_access", "register_backups", "register_catalog", "register_domain_lists", "register_history", "register_service", "register_sources", "validate_start"]
