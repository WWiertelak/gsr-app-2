"""The interview moments come from the Analysis results: drop the Interviews step's own VSR settings.

``Project.interview_config["vsr"]`` held separate thresholds and windows; only the number
of moments stays, as ``primary_moment_count`` / ``reserve_moment_count``. Moments stored
by the removed engine (``AnalysisResult`` kind ``vsr``) are deleted: the screen asks to
choose them again from the Analysis. The researcher's decided and manual moments
(``InterviewMoment``) stay; choosing the moments again keeps them.
"""

from django.db import migrations

DEFAULTS = {"primary_moment_count": (5, 1, 20), "reserve_moment_count": (3, 0, 20)}
OLD_KEYS = {"primary_moment_count": "vsr_primary_moment_count", "reserve_moment_count": "vsr_reserve_moment_count"}


def _count(value, default, low, high):
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default
    if isinstance(value, bool) or not number.is_integer() or not low <= number <= high:
        return default
    return int(number)


def forwards(apps, schema_editor):
    Project = apps.get_model("gsr", "Project")
    AnalysisResult = apps.get_model("gsr", "AnalysisResult")
    for project in Project.objects.exclude(interview_config={}):
        stored = dict(project.interview_config or {})
        old = stored.pop("vsr", None) or {}
        for name, (default, low, high) in DEFAULTS.items():
            stored[name] = _count(stored.get(name, old.get(OLD_KEYS[name], default)), default, low, high)
        project.interview_config = stored
        project.save(update_fields=["interview_config"])
    AnalysisResult.objects.filter(kind="vsr").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("gsr", "0004_interview_moment"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
