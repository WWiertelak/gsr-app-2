from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path_factory, settings):
    root = tmp_path_factory.mktemp("storage")
    settings.STORAGE_ROOT = root
    settings.MEDIA_ROOT = root
    return root
