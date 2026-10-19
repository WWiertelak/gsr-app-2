"""The synchrony types GSRv2 did not have: divergent synchrony and decoupling (Hassan's e-mail; the VSR flow chart
read by strength)."""

from __future__ import annotations

import numpy as np
import pytest

from gsr.pipeline.config import PipelineConfig
from gsr.pipeline.synchrony import decoupled_episodes, divergent_episodes, synchrony_types

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


def test_synchrony_types_add_the_divergent_and_decoupling_episodes_and_their_counts() -> None:
    synchrony = _synchrony([(100, 160, -0.6)], end=400.0)

    typed = synchrony_types(synchrony, CONFIG)

    assert typed["episodes"] == [] and len(typed["divergent_episodes"]) == 1
    # Before and after the divergent episode: two decoupled stretches (r = 0 there).
    assert [(round(e["start_relative"] * 60), round(e["end_relative"] * 60)) for e in typed["decoupled_episodes"]] \
        == [(15, 95), (165, 385)]
    assert typed["stats"] == {"episode_count": 0, "divergent_episode_count": 1, "decoupled_episode_count": 2}
    assert "divergent_episodes" not in synchrony  # the GSRv2 payload is left as it was


def _decoupled_spans(synchrony: dict, config: PipelineConfig = CONFIG) -> list[tuple[int, int, float]]:
    return [(round(e["start_relative"] * 60), round(e["end_relative"] * 60), round(e["mean_r"], 3))
            for e in decoupled_episodes(synchrony, config)]


def test_decoupling_is_one_correlation_for_the_whole_stretch_between_the_episodes() -> None:
    # Hassan: "the correlation between GSR data within an episode is between -.10 and +.10". The windowed r swings
    # (+0.6, -0.6, ...) so no 30 s run of windows has |r| < 0.1, but the stretch as a whole averages to zero.
    synchrony = _synchrony([], end=400.0)
    centers = synchrony["windows"]["centers"]
    synchrony["windows"]["pairs"][0]["values"] = [0.6 if index % 2 else -0.6 for index in range(len(centers))]
    synchrony["episodes"] = [{"start_relative": 200 / 60, "end_relative": 240 / 60, "members": ["A", "B"]}]

    spans = _decoupled_spans(synchrony)

    assert [(start, end) for start, end, _mean in spans] == [(15, 195), (245, 385)]
    assert all(abs(mean) < 0.1 for _start, _end, mean in spans)


def test_decoupling_needs_a_mean_within_the_threshold_and_the_minimum_duration() -> None:
    # Stretches (outside the synchronization episodes): 15-95 at 0.05, 165-185 too short, 255-385 at 0.3.
    synchrony = _synchrony([(15, 95, 0.05), (255, 385, 0.3)], end=400.0)
    synchrony["episodes"] = [
        {"start_relative": 100 / 60, "end_relative": 160 / 60, "members": ["A", "B"]},
        {"start_relative": 190 / 60, "end_relative": 250 / 60, "members": ["B", "A", "C"]},
        {"start_relative": 300 / 60, "end_relative": 320 / 60, "members": ["A", "C"]},  # not this pair's
    ]

    assert [(start, end) for start, end, _mean in _decoupled_spans(synchrony)] == [(15, 95)]
    wider = CONFIG.model_copy(update={"synchrony_decoupled_threshold": 0.35})
    assert [(start, end) for start, end, _mean in _decoupled_spans(synchrony, wider)] == [(15, 95), (255, 385)]


def test_decoupling_is_found_for_every_pair_of_a_group() -> None:
    synchrony = _synchrony([], end=200.0)
    values = synchrony["windows"]["pairs"][0]["values"]
    synchrony["aliases"] = ["A", "B", "C"]
    synchrony["windows"]["pairs"] = [
        {"alias_a": "A", "alias_b": "B", "values": values},
        {"alias_a": "A", "alias_b": "C", "values": [0.5] * len(values)},
        {"alias_a": "B", "alias_b": "C", "values": values},
    ]

    episodes = decoupled_episodes(synchrony, CONFIG)

    assert [episode["members"] for episode in episodes] == [["A", "B"], ["B", "C"]]
    assert all(episode["size"] == 2 for episode in episodes)
