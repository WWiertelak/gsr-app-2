"""Remove video offsets from stored configuration and proposals; preserve sensor clocks and curation.

Existing VSR signatures become outdated once, because they included the removed
setting. Choosing moments again refreshes the signature and retains decisions.
"""

from django.db import migrations

VIDEO_KEYS = {"video_start_time", "start_video", "end_video"}


def without_video(value):
    if isinstance(value, dict):
        return {key: without_video(item) for key, item in value.items() if key not in VIDEO_KEYS}
    if isinstance(value, list):
        return [without_video(item) for item in value]
    return value


def forwards(apps, schema_editor):
    for model_name, fields in (
        ("Project", ("interview_config",)),
        ("AnalysisResult", ("config", "payload")),
        ("InterviewMoment", ("proposal",)),
    ):
        model = apps.get_model("gsr", model_name)
        for row in model.objects.iterator():
            changed = []
            for field in fields:
                before = getattr(row, field)
                after = without_video(before)
                if before != after:
                    setattr(row, field, after)
                    changed.append(field)
            if changed:
                row.save(update_fields=changed)


class Migration(migrations.Migration):
    dependencies = [("gsr", "0005_interview_moments_from_analysis")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
