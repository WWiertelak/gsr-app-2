"""The synchrony types GSRv2 did not have: divergent synchrony (Hassan's e-mail; the VSR flow chart read by strength)."""

from __future__ import annotations

import numpy as np
import pytest

from gsr.pipeline.config import PipelineConfig
from gsr.pipeline.synchrony import divergent_episodes, synchrony_types

CONFIG = PipelineConfig()  # window 30 s, step 5 s, episodes at least 30 s, gaps up to 15 s bridged; divergent r <= -0.3


def _synchrony(spans: list[tuple[float, float, float]], aliases: tuple[str, str] = ("A", "B"),
               default: float = 0.0, end: float = 1800.0) -> dict:
    """A one-pair synchronization payload: windows every 5 s from 15 s, r = ``default`` except in (start, end, r)."""
    centers = np.arange(15.0, end - 14.0, 5.0)
    values = np.full(len(centers), default)
    for start, stop, value in spans:
        values[(centers >= start) & (centers <= stop)] = value
    return {
        "aliases": list(aliases),
        "windows": {"centers": list(centers / 60.0),
                    "pairs": [{"alias_a": aliases[0], "alias_b": aliases[1], "values": list(values)}]},
        "episodes": [],
        "stats": {"episode_count": 0},
    }


def test_divergent_episodes_are_windows_at_or_below_minus_the_threshold() -> None:
    synchrony = _synchrony([(100, 160, -0.6), (300, 320, -0.9), (400, 460, -0.29), (600, 660, -0.3),
                            (800, 860, 0.9)])

    episodes = divergent_episodes(synchrony, CONFIG)

    # 300-320 is shorter than 30 s; -0.29 is not strong enough; -0.3 is; a positive stretch is synchrony, not this.
    assert [(round(e["start_relative"] * 60), round(e["end_relative"] * 60)) for e in episodes] == [
        (100, 160), (600, 660)]
    assert episodes[0]["members"] == ["A", "B"] and episodes[0]["size"] == 2
    assert episodes[0]["mean_strength"] == pytest.approx(-0.6) and episodes[1]["peak_strength"] == pytest.approx(-0.3)


def test_divergent_episodes_follow_the_synchronization_episode_rules() -> None:
    # A 10 s dip back to -0.1 is bridged (gaps up to 15 s), a 30 s one is not.
    synchrony = _synchrony([(100, 140, -0.8), (145, 150, -0.1), (155, 200, -0.8),
                            (300, 340, -0.8), (345, 370, -0.1), (375, 420, -0.8)])

    episodes = divergent_episodes(synchrony, CONFIG)

    assert [(round(e["start_relative"] * 60), round(e["end_relative"] * 60)) for e in episodes] == [
        (100, 200), (300, 340), (375, 420)]
    stricter = divergent_episodes(synchrony, CONFIG.model_copy(update={"synchrony_divergent_threshold": 0.85}))
    assert stricter == []


def test_the_unsigned_mode_has_no_divergent_episodes() -> None:
    # Without the signed mode an opposite-direction pair already counts as synchronized.
    synchrony = _synchrony([(100, 160, -0.9)])

    assert divergent_episodes(synchrony, CONFIG.model_copy(update={"synchrony_signed": False})) == []


def test_synchrony_types_add_the_divergent_episodes_and_their_count() -> None:
    synchrony = _synchrony([(100, 160, -0.6)])

    typed = synchrony_types(synchrony, CONFIG)

    assert typed["episodes"] == [] and len(typed["divergent_episodes"]) == 1
    assert typed["stats"] == {"episode_count": 0, "divergent_episode_count": 1}
    assert "divergent_episodes" not in synchrony  # the GSRv2 payload is left as it was
