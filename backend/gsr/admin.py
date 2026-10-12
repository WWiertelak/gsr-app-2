from django.contrib import admin

from .models import Dataset, Project


class DatasetInline(admin.TabularInline):
    model = Dataset
    extra = 0
    fields = ("alias", "original_filename", "file_size", "created_at")
    readonly_fields = ("alias", "original_filename", "file_size", "created_at")
    can_delete = True


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at", "updated_at")
    search_fields = ("name",)
    inlines = [DatasetInline]


@admin.register(Dataset)
class DatasetAdmin(admin.ModelAdmin):
    list_display = ("alias", "project", "original_filename", "file_size", "created_at")
    list_filter = ("project",)
    search_fields = ("alias", "original_filename")
    readonly_fields = ("file_sha256", "file_size", "parsed_metadata", "created_at")
