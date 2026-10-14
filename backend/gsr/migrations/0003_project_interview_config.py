from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("gsr", "0002_ui_results"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="interview_config",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name="analysisresult",
            name="kind",
            field=models.CharField(
                choices=[
                    ("cleaning", "Cleaning preview"),
                    ("analysis", "Synchronization and co-arousal"),
                    ("vsr", "VSR interview moments"),
                ],
                max_length=20,
            ),
        ),
    ]
