"""OpenProject API client layer (SPEC §4).

Import surface for tool modules::

    from openproject_mcp.client import OpenProjectClient, TTLCache
    from openproject_mcp.client.errors import NotFoundError, ValidationFailedError
    from openproject_mcp.client.filters import make_filter, query_params, status_filter
    from openproject_mcp.client.hal import collection, formattable, ref, self_id
    from openproject_mcp.client.locking import patch_with_lock
    from openproject_mcp.client.payloads import build_write_payload, links_payload
"""

from openproject_mcp.client.cache import TTLCache, credential_scope
from openproject_mcp.client.errors import OpenProjectError
from openproject_mcp.client.http import OpenProjectClient

__all__ = ["OpenProjectClient", "OpenProjectError", "TTLCache", "credential_scope"]
