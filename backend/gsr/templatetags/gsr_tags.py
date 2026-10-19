from __future__ import annotations

from datetime import datetime
from typing import Any

from django import template

from ..pipeline.common import clock_at

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
def seconds_clock(seconds: Any, start: datetime | None) -> str:
    """Lesson seconds → the recordings' own clock (HH:MM:SS); ``start`` is the clock at lesson time 0."""
    if not isinstance(seconds, (int, float)):
        return "—"
    return clock_at(start, seconds) or "—"


@register.filter
def minutes_clock(minutes: Any, start: datetime | None) -> str:
    """Lesson minutes → the recordings' own clock (HH:MM:SS), like ``mmss`` gives the lesson time."""
    if not isinstance(minutes, (int, float)):
        return "—"
    return seconds_clock(minutes * 60.0, start)


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
def display_number(value: Any) -> str:
    """A setting value for display text, in the UI language's decimal format ("0,6" in Polish)."""
    from ..params import display_number as display  # params imports this module

    return display(value)


@register.filter
def choice_label(value: Any, name: str) -> Any:
    """``{{ value|choice_label:"synchrony_surrogate_method" }}``: the translated label of a choice setting."""
    from ..params import choice_label as label  # params imports this module

    return label(name, value)


@register.filter
def get_item(mapping: Any, key: Any) -> Any:
    try:
        return mapping.get(key)
    except AttributeError:
        return None
