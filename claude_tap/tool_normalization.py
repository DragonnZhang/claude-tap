"""Normalize provider tool definitions into model-callable leaf tools."""

from __future__ import annotations

from typing import Any


def expand_tool_namespaces(tools: Any, namespace: str = "") -> list[dict[str, Any]]:
    """Flatten OpenAI namespace tools while preserving qualified callable names."""

    if not isinstance(tools, list):
        return []

    expanded: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "")
        qualified_name = ".".join(part for part in (namespace, name) if part)
        children = tool.get("tools")
        if tool.get("type") == "namespace" and isinstance(children, list) and children:
            expanded.extend(expand_tool_namespaces(children, qualified_name))
            continue
        expanded.append({**tool, "name": qualified_name} if namespace and qualified_name else tool)
    return expanded
