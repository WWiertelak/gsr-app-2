from __future__ import annotations

import pytest
from django.conf import settings

from gsr.checks import check_dtaidistance_c_library, dtw_c_library_available


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
