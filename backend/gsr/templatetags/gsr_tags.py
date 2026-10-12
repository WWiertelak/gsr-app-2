from __future__ import annotations

from typing import Any

from django import template

register = template.Library()


@register.filter
def mmss(minutes: Any) -> str:
    """Relative minutes (float) → m:ss, as in GSRv2's time labels."""
    if not isinstance(minutes, (int, float)):
        return "—"
    total_seconds = max(0, round(minutes * 60))
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


@register.filter
def seconds_mmss(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "—"
    return mmss(seconds / 60.0)


@register.filter
def input_value(value: Any) -> str:
    """A setting value as an <input> value: dot decimals regardless of the UI language."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value)) if abs(value) < 1e15 else repr(value)
    return str(value)


@register.filter
def get_item(mapping: Any, key: Any) -> Any:
    try:
        return mapping.get(key)
    except AttributeError:
        return None
