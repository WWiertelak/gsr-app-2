from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Django's set_language view backs the PL/EN switch in the header.
    path("i18n/", include("django.conf.urls.i18n")),
    path("", include("gsr.urls")),
]
