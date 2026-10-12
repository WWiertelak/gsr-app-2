"""Generated SECRET_KEY: created once with private permissions, then reused."""

from __future__ import annotations

import stat

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.settings import _load_or_create_secret_key


def test_secret_key_is_generated_once_and_reused(tmp_path):
    path = tmp_path / "secret_key"

    key = _load_or_create_secret_key(path)

    assert len(key) >= 50
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _load_or_create_secret_key(path) == key


def test_empty_secret_key_file_is_rejected(tmp_path):
    path = tmp_path / "secret_key"
    path.write_text("\n")

    with pytest.raises(ImproperlyConfigured):
        _load_or_create_secret_key(path)
