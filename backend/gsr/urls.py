from django.urls import path

from . import views

app_name = "gsr"

urlpatterns = [
    path("", views.project_list, name="projects"),
    path("projects/<uuid:pk>/", views.project_detail, name="project"),
    path("projects/<uuid:pk>/edit/", views.project_update, name="project_update"),
    path("projects/<uuid:pk>/delete/", views.project_delete, name="project_delete"),
    path("projects/<uuid:pk>/upload/", views.upload, name="upload"),
    path("projects/<uuid:pk>/upload/overview/", views.upload_overview, name="upload_overview"),
    path("projects/<uuid:pk>/upload/window/", views.upload_window, name="upload_window"),
    path("projects/<uuid:pk>/datasets/<uuid:dataset_pk>/rename/", views.dataset_rename, name="dataset_rename"),
    path("projects/<uuid:pk>/datasets/<uuid:dataset_pk>/delete/", views.dataset_delete, name="dataset_delete"),
    path("projects/<uuid:pk>/datasets/<uuid:dataset_pk>/exclude/", views.dataset_exclude, name="dataset_exclude"),
    path("projects/<uuid:pk>/cleaning/", views.cleaning, name="cleaning"),
    path("projects/<uuid:pk>/cleaning/refresh/", views.cleaning_refresh, name="cleaning_refresh"),
    path("projects/<uuid:pk>/cleaning/reset/", views.cleaning_reset, name="cleaning_reset"),
    path("projects/<uuid:pk>/cleaning/clean_signals.csv", views.cleaning_csv, name="cleaning_csv"),
    path("projects/<uuid:pk>/analysis/", views.analysis, name="analysis"),
    path("projects/<uuid:pk>/analysis/reset/", views.analysis_reset, name="analysis_reset"),
    path("projects/<uuid:pk>/interviews/", views.interviews, name="interviews"),
    path("projects/<uuid:pk>/interviews/settings/", views.interviews_settings, name="interviews_settings"),
    path("projects/<uuid:pk>/interviews/reset/", views.interviews_reset, name="interviews_reset"),
    path("projects/<uuid:pk>/export/", views.export, name="export"),
    path("projects/<uuid:pk>/export/rerun/", views.export_rerun, name="export_rerun"),
    path("projects/<uuid:pk>/export/report.pdf", views.export_pdf, name="export_pdf"),
    path("projects/<uuid:pk>/export/results.zip", views.export_zip, name="export_zip"),
    path("projects/<uuid:pk>/export/files/<str:name>", views.export_file, name="export_file"),
]
