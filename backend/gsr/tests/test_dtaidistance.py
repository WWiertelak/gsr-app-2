from __future__ import annotations

import pytest
from django.conf import settings
from django.core.checks import Error
from django.core.checks.registry import registry
from django.core.management import call_command
from django.core.management.base import SystemCheckError

from gsr.checks import DTW_C_LIBRARY_TAG, check_dtaidistance_c_library, dtw_c_library_available


@pytest.mark.skipif(not settings.GSR_REQUIRE_DTW_C, reason="GSR_REQUIRE_DTW_C is disabled")
def test_dtaidistance_c_library_is_available() -> None:
    assert dtw_c_library_available(), (
        "dtaidistance.dtw.try_import_c() failed; rebuild the image with the compiled C extension "
        "(see backend/Dockerfile)."
    )


@pytest.mark.skipif(not settings.GSR_REQUIRE_DTW_C, reason="GSR_REQUIRE_DTW_C is disabled")
def test_dtaidistance_c_library_system_check() -> None:
    errors = check_dtaidistance_c_library(None)
    assert not errors, errors[0].msg if errors else ""


def test_tagged_check_fails_when_c_library_missing(monkeypatch, settings) -> None:
    settings.GSR_REQUIRE_DTW_C = True
    monkeypatch.setattr("gsr.checks.dtw_c_library_available", lambda: False)
    with pytest.raises(SystemCheckError, match="gsr.E001"):
        call_command("check", tags=[DTW_C_LIBRARY_TAG], fail_level="ERROR")


def test_tagged_check_ignores_unrelated_error_checks(monkeypatch, settings) -> None:
    settings.GSR_REQUIRE_DTW_C = True
    monkeypatch.setattr("gsr.checks.dtw_c_library_available", lambda: True)

    def unrelated_error(app_configs, **kwargs):
        return [Error("unrelated", id="test.E001")]

    unrelated_error.tags = ()
    monkeypatch.setattr(
        registry, "registered_checks", registry.registered_checks | {unrelated_error}
    )
    with pytest.raises(SystemCheckError, match="test.E001"):
        call_command("check", fail_level="ERROR")
    call_command("check", tags=[DTW_C_LIBRARY_TAG], fail_level="ERROR")
