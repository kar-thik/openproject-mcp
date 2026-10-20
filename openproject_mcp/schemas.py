"""Wire-size control for tool output schemas (SPEC §5.4).

An output schema exists so a client can validate ``structuredContent``. Its
field descriptions duplicate the result models' prose on every tool that
returns a shared projection, and made up about half of ``tools/list``. The
model chooses tools from the description and the *input* schema, so
:class:`CompactOutputSchemas` rewrites output schemas only: it drops the
``description`` and ``default`` annotations and folds ``anyOf`` lists of bare
types into one ``type`` array. What validates is unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool

__all__ = ["CompactOutputSchemas", "compact_schema"]

#: Keywords whose value maps names to subschemas; the names are data, not keywords.
_SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions"})

#: Annotations that never affect validation.
_DROPPED = frozenset({"description", "default"})

#: Keywords whose value is instance data, never a subschema.
_DATA_KEYWORDS = frozenset({"examples", "const", "enum", "required"})


def _bare_types(branches: list[Any]) -> list[str] | None:
    """``[{"type": "string"}, {"type": "null"}]`` → ``["string", "null"]``, else None."""
    types: list[str] = []
    for branch in branches:
        if not (isinstance(branch, dict) and branch.keys() == {"type"}):
            return None
        kind = branch["type"]  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(kind, str):
            return None
        types.append(kind)
    return types


def compact_schema(schema: Any) -> Any:
    """Return ``schema`` without annotations and with bare-type unions folded."""
    if isinstance(schema, list):
        return [compact_schema(item) for item in schema]  # pyright: ignore[reportUnknownVariableType]
    if not isinstance(schema, dict):
        return schema
    compact: dict[str, Any] = {}
    for key, value in schema.items():  # pyright: ignore[reportUnknownVariableType]
        if key in _DROPPED:
            continue
        if key in _DATA_KEYWORDS:
            compact[key] = value
        elif key in _SCHEMA_MAPS and isinstance(value, dict):
            compact[key] = {name: compact_schema(sub) for name, sub in value.items()}  # pyright: ignore[reportUnknownVariableType]
        else:
            compact[key] = compact_schema(value)
    branches = compact.get("anyOf")
    if isinstance(branches, list) and "type" not in compact:
        types = _bare_types(branches)  # pyright: ignore[reportUnknownArgumentType]
        if types is not None:
            del compact["anyOf"]
            compact["type"] = types
    return compact


class CompactOutputSchemas(Middleware):
    """Strip prose from every listed tool's output schema."""

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        return [
            tool
            if tool.output_schema is None
            else tool.model_copy(update={"output_schema": compact_schema(tool.output_schema)})
            for tool in tools
        ]
