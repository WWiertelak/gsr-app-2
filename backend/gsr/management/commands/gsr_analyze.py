from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from gsr.pipeline.config import PipelineConfig
from gsr.services.analysis import ANALYSES, AnalysisError, analyze_files


class Command(BaseCommand):
    help = (
        "Clean and synchronize eSense GSR CSV files and run joint synchronization and "
        "co-arousal, without touching the database. Example: "
        "manage.py gsr_analyze A=a.csv B=b.csv --config '{\"scaling_enabled\": true}'"
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("datasets", nargs="+", help="ALIAS=PATH pairs, one per recording")
        parser.add_argument("--config", default="{}", help="PipelineConfig overrides as a JSON object")
        parser.add_argument(
            "--analyses",
            nargs="*",
            choices=ANALYSES,
            default=list(ANALYSES),
            help="Analyses to run (default: all); pass none for cleaning/synchronization only",
        )
        parser.add_argument("--output", help="Write the JSON result here instead of stdout")

    def handle(self, *args, **options) -> None:
        files: dict[str, Path] = {}
        for item in options["datasets"]:
            alias, separator, path = item.partition("=")
            if not separator or not alias or not path:
                raise CommandError(f"Expected ALIAS=PATH, got {item!r}")
            if alias in files:
                raise CommandError(f"Duplicate alias: {alias}")
            files[alias] = Path(path)

        try:
            overrides = json.loads(options["config"])
        except json.JSONDecodeError as exc:
            raise CommandError(f"--config is not valid JSON: {exc}") from exc
        if not isinstance(overrides, dict):
            raise CommandError("--config must be a JSON object")
        try:
            config = PipelineConfig(**overrides)
        except (TypeError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        try:
            payload = analyze_files(files, config, options["analyses"])
        except AnalysisError as exc:
            raise CommandError(str(exc)) from exc

        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if options["output"]:
            Path(options["output"]).write_text(text, encoding="utf-8")
        else:
            self.stdout.write(text)
