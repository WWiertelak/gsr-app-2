from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret-key")
DEBUG = os.getenv("DEBUG", "1") == "1"
if not DEBUG and SECRET_KEY == "dev-insecure-secret-key":
    raise ImproperlyConfigured("SECRET_KEY must be set in production")
ALLOWED_HOSTS_DEFAULT = "*" if DEBUG else "localhost"
ALLOWED_HOSTS = [
    host.strip() for host in os.getenv("ALLOWED_HOSTS", ALLOWED_HOSTS_DEFAULT).split(",") if host.strip()
]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = not DEBUG and os.getenv("SECURE_SSL_REDIRECT", "1") == "1"
SESSION_COOKIE_SECURE = not DEBUG and os.getenv("SECURE_COOKIES", "1") == "1"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000" if not DEBUG else "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "gsr",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves collected static files (admin, UI) under gunicorn in the container.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # UI language from the PL/EN switch (cookie), then Accept-Language.
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "gsr.context_processors.ui",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": Path(os.getenv("SQLITE_PATH", BASE_DIR / "db.sqlite3")),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Polish is the default UI language, as in GSRv2; the header switch selects English.
LANGUAGE_CODE = "pl"
LANGUAGES = [("pl", "Polski"), ("en", "English")]
LOCALE_PATHS = [BASE_DIR / "gsr" / "locale"]
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Uploaded GSR files live under STORAGE_ROOT (served as MEDIA_ROOT).
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", str(BASE_DIR / "storage"))).resolve()
MEDIA_ROOT = STORAGE_ROOT
MEDIA_URL = "media/"

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "100"))
# The pipeline reads the same MAX_DATASETS variable directly (gsr.pipeline.config).
MAX_DATASETS = int(os.getenv("MAX_DATASETS", "20"))

DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_MB * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_MB * 1024 * 1024
