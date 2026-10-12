from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from .charts import chart_labels


def ui(request: HttpRequest) -> dict[str, Any]:
    return {"chart_labels": chart_labels()}
