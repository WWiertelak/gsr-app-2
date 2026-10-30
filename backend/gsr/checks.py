"""Django system checks for deployment requirements."""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, register


def dtw_c_library_available() -> bool:
    try:
        from dtaidistance.dtw import try_import_c
    except ImportError:
        return False
    return bool(try_import_c())


@register()
def check_dtaidistance_c_library(app_configs, **kwargs):
    if not getattr(settings, "GSR_REQUIRE_DTW_C", False):
        return []
    if dtw_c_library_available():
        return []
    return [
        Error(
            "The compiled dtaidistance C library is not available; synchrony DTW falls back to "
            "pure Python and class-sized analyses exceed the HTTP worker timeout. Rebuild the "
            "image with build-essential and reinstall dtaidistance from source "
            "(see backend/Dockerfile).",
            id="gsr.E001",
        )
    ]
