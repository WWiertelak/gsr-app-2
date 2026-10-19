"""The VSR interview moments in words, in the UI language (or the report language).

A moment's justification is one plain sentence built from the Analysis values it was
chosen from: the step of the VSR Interview Flow Chart it answers, the Analysis episode,
its times, the correlation r and who was in the shared arousal state. The Interviews
screen, the PDF report and the CSV use the same sentence.
"""

from __future__ import annotations

from typing import Any

from django.utils.formats import number_format
from django.utils.text import capfirst
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from .pipeline.vsr import AGGREGATE, COMOVEMENT_STATE, HIGH, STEP_NUMBERS, SYNCHRONY, step_label
from .templatetags.gsr_tags import seconds_mmss


def step_name(step: Any, state: str | None = None) -> str:
    """The step's name in lower case; STEP 1 names the shared state when it is known."""
    step = step_label(step)
    if step == "1":
        if state == HIGH:
            return _("co-movement and shared High arousal")
        if state is not None:
            return _("co-movement and shared Low arousal")
        return _("co-movement and a shared arousal state")
    return {
        "2": _("co-movement only"),
        "3": _("shared High arousal"),
        "4": _("shared Low arousal"),
        "4b": _("divergent synchrony"),
        "5": _("no synchrony"),
    }[step]


def step_title(step: Any) -> str:
    """The step's number and name for a heading or a badge: "Step 2 · Co-movement only"."""
    return _("Step %(number)s · %(name)s") % {"number": step_label(step), "name": capfirst(step_name(step))}


def steps() -> list[dict[str, Any]]:
    return [{"number": step, "name": capfirst(step_name(step))} for step in STEP_NUMBERS]


def correlation(value: float | None) -> str:
    """r to two decimals, rounded as the steps compare it (``number_format`` alone would cut -0.9499 to -0.94)."""
    return "—" if value is None else number_format(round(value, 2), 2)


def _times(start: float, end: float) -> str:
    return f"{seconds_mmss(start)}–{seconds_mmss(end)}"


def _episode(episode: dict[str, Any], times: str) -> str:
    kind = episode.get("kind") or ""
    values = {"number": episode["number"], "times": times}
    if episode["source"] == AGGREGATE:
        if kind == "divergent":
            return _("divergent synchrony of the teacher and the students' average, %(times)s") % values
        if kind == "decoupled":
            return _("decoupling of the teacher and the students' average, %(times)s") % values
        if kind == "opposite":
            return _("opposite arousal of the teacher and the students' average, %(times)s") % values
        if episode.get("state") is None:
            return _("co-movement of the teacher and the students' average, %(times)s") % values
        return _("co-arousal of the teacher and the students' average, %(times)s") % values
    if episode["source"] == SYNCHRONY:
        if kind == "divergent":
            return _("divergent synchronization episode D%(number)s, %(times)s") % values
        if kind == "decoupled":
            return _("decoupling stretch U%(number)s, %(times)s") % values
        return _("synchronization episode E%(number)s, %(times)s") % values
    if episode.get("state") is None and kind != "opposite":
        return _("co-movement episode %(number)s of the co-arousal, %(times)s") % values
    return _("co-arousal episode %(number)s, %(times)s") % values


def shared_state(proposal: dict[str, Any], kind: str) -> str | None:
    """Who was in the shared arousal state: both people of a pair, the teacher and the students' average, or (moments
    chosen before the teacher interview used the students' average) the teacher and how many students."""
    state = proposal.get("shared_state")
    if state is None:
        return None
    if kind != "teacher":
        return _("both people in the High state") if state == HIGH else _("both people in the Low state")
    step_one = proposal.get("type") == COMOVEMENT_STATE or step_label(proposal.get("step")) == "1"
    state_episode = proposal.get("shared_episode") if step_one else proposal.get("episode")
    if (state_episode or {}).get("source") == AGGREGATE:
        return (_("the teacher and the students' average in the High state") if state == HIGH
                else _("the teacher and the students' average in the Low state"))
    values = {"count": proposal["in_state"], "total": proposal["people"]}
    if state == HIGH:
        return ngettext("the teacher and %(count)s of %(total)s student in the High state",
                        "the teacher and %(count)s of %(total)s students in the High state", values["total"]) % values
    return ngettext("the teacher and %(count)s of %(total)s student in the Low state",
                    "the teacher and %(count)s of %(total)s students in the Low state", values["total"]) % values


def _details(proposal: dict[str, Any], kind: str) -> list[str]:
    step = step_label(proposal["step"])
    times = _times(proposal["start_seconds"], proposal["end_seconds"])
    r = f"r = {correlation(proposal['correlation'])}"
    if step in ("1", "2"):
        return [_episode(proposal["episode"], times), r,
                shared_state(proposal, kind) if step == "1" else _("no shared arousal state")]
    if step in ("3", "4"):
        return [_episode(proposal["episode"], times), shared_state(proposal, kind), r, _("no co-movement")]
    if step == "4b":
        return [_episode(proposal["episode"], times), r, _("the signals moved in opposite directions")]
    if proposal.get("type") == "opposite":
        who = (_("the teacher or the students' average in the High state, the other in the Low state")
               if kind == "teacher" else _("one person in the High state, the other in the Low state"))
        return [_episode(proposal["episode"], times), who, r]
    if proposal.get("type"):
        return [_episode(proposal["episode"], times), r, _("the signals were not related")]
    # STEP 5 of moments chosen when it took a correlation below 0.1 with its sign.
    relation = (_("the signals moved in the opposite direction") if proposal["correlation"] < 0
                else _("the signals were not related"))
    if proposal.get("episode"):
        return [_episode(proposal["episode"], times), r, relation]
    return [_("%(times)s outside the synchronization and co-arousal episodes") % {"times": times}, r, relation]


def justification(proposal: dict[str, Any], kind: str) -> str:
    """One sentence: why the moment was chosen, from the Analysis values (``proposal`` as ``pipeline/vsr`` stores it).

    ``kind`` is the interview's kind ("dyad" or "teacher").
    """
    step = proposal.get("step")
    if step is None or step_label(step) not in STEP_NUMBERS:
        return ""
    head = _("VSR flow chart step %(step)s: %(name)s") % {"step": step_label(step),
                                                          "name": step_name(step, proposal.get("shared_state"))}
    try:
        details = [detail for detail in _details(proposal, kind) if detail]
    except (AttributeError, KeyError, TypeError):  # a moment stored before the moments came from the Analysis
        details = []
    return f"{head} – {', '.join(details)}." if details else f"{head}."
