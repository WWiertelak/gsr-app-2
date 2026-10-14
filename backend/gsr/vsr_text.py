"""VSR justification fragments (``pipeline/vsr.py``) as sentences in the UI language.

Every document quote stays verbatim in the document's English, marked up as a
``<q>`` with its page; only the words around the quotes are translated. The
sentences are safe HTML, so the Interviews screen and the PDF report can show
the same text.
"""

from __future__ import annotations

from typing import Any

from django.utils.formats import number_format
from django.utils.html import conditional_escape, format_html
from django.utils.safestring import SafeString, mark_safe
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from .pipeline.vsr import CANDIDATES_EXHAUSTED, HIGH, HIGHEST_CORRELATION, LOWEST_CORRELATION, OPPOSITE_DIRECTION


def quote(item: dict[str, Any], ellipsis: bool = False) -> SafeString:
    """One document quote: the verbatim English text and the page it is on."""
    return format_html(
        '<q class="doc-quote" lang="en">{}{}</q> <span class="doc-page">({})</span>',
        item["text"],
        "…" if ellipsis else "",
        _("p. %(page)s") % {"page": item["page"]},
    )


def _sentence(template: str, **values: Any) -> SafeString:
    """``template`` (a translated %-format string) with HTML-escaped ``values``; quotes come in already safe."""
    return mark_safe(conditional_escape(template) % {key: conditional_escape(value) for key, value in values.items()})


def correlation(value: float) -> str:
    return number_format(value, 2)


def percent(fraction: float) -> str:
    return number_format(round(fraction * 100))


def _project_threshold(fragment: dict[str, Any]) -> str:
    if fragment["threshold"] == fragment["document_threshold"]:
        return ""
    return _(" (project threshold: %(value)s)") % {"value": number_format(fragment["threshold"])}


def _project_target(fragment: dict[str, Any]) -> str:
    if fragment["target"] == fragment["document_target"]:
        return ""
    return ngettext(
        " (project target: %(count)s moment)", " (project target: %(count)s moments)", fragment["target"]
    ) % {"count": fragment["target"]}


def _rule(rule: str) -> str:
    """What the chosen moment has the most of ("this one has ...")."""
    if rule == HIGHEST_CORRELATION:
        return _("the highest correlation")
    if rule == LOWEST_CORRELATION:
        return _("the lowest correlation")
    return _("the largest share of the shared arousal state")


def _order(rule: str) -> str:
    """How the moments of one step are ranked ("ordered ...")."""
    if rule == HIGHEST_CORRELATION:
        return _("highest correlation first")
    if rule == LOWEST_CORRELATION:
        return _("lowest correlation first")
    return _("largest shared arousal state share first")


def step_sentence(fragment: dict[str, Any]) -> SafeString:
    """A STEP 1-5 question with its answer and the document's instruction for that answer."""
    if fragment["answer"] == "YES":
        return _sentence(
            _("STEP %(step)s: %(question)s → YES → %(outcome)s"),
            step=fragment["step"],
            question=quote(fragment["quote"]),
            outcome=quote(fragment["outcome"]),
        )
    if fragment["reason"] == CANDIDATES_EXHAUSTED:
        detail = ngettext(
            "%(count)s moment meets the criterion, already selected or overlapping a selected moment",
            "%(count)s moments meet the criterion, all already selected or overlapping a selected moment",
            fragment["candidate_count"],
        ) % {"count": fragment["candidate_count"]}
    else:
        detail = _("no moment meets the criterion")
    return _sentence(
        _("STEP %(step)s: %(question)s → NO (%(detail)s) → %(outcome)s"),
        step=fragment["step"],
        question=quote(fragment["quote"]),
        detail=detail,
        outcome=quote(fragment["outcome"]),
    )


def step_6_sentence(record: dict[str, Any]) -> SafeString:
    """STEP 6 at the end of one pass: have enough moments been selected?"""
    answer = _("YES") if record["answer"] == "YES" else _("NO")
    return _sentence(
        _("STEP 6: %(question)s%(target)s → %(answer)s (%(count)s selected) → %(instruction)s"),
        question=quote(record["question"]),
        target=_project_target(record),
        answer=answer,
        count=record["selected_count"],
        instruction=quote(record["instruction"]),
    )


def justification(fragments: list[dict[str, Any]]) -> list[SafeString]:
    """The sentences of one proposal's justification, in fragment order."""
    sentences = []
    for fragment in fragments:
        kind = fragment["kind"]
        if kind == "data_basis":
            sentences.append(_sentence(_("Data: %(quote)s."), quote=quote(fragment["quote"])))
        elif kind == "step_question":
            sentences.append(step_sentence(fragment))
        elif kind == "synchrony_type":
            sentences.append(_sentence(_("Synchrony type: %(quote)s."), quote=quote(fragment["quote"])))
        elif kind == "criterion":
            template = (
                _("Criterion %(quote)s%(project)s: r = %(r)s.")
                if fragment["meets_threshold"]
                else _("Criterion %(quote)s%(project)s not met (r = %(r)s): this separates the type from co-movement.")
            )
            sentences.append(
                _sentence(
                    template,
                    quote=quote(fragment["quote"]),
                    project=_project_threshold(fragment),
                    r=correlation(fragment["correlation"]),
                )
            )
        elif kind == "shared_state":
            template = (
                _(
                    "Both participants in the HIGH arousal state for %(share)s%% of the moment (a shared "
                    "state needs %(minimum)s%%)."
                )
                if fragment["state"] == HIGH
                else _(
                    "Both participants in the LOW arousal state for %(share)s%% of the moment (a shared "
                    "state needs %(minimum)s%%)."
                )
            )
            sentences.append(
                _sentence(template, share=percent(fragment["fraction"]), minimum=percent(fragment["minimum_fraction"]))
            )
        elif kind == "no_shared_state":
            sentences.append(_sentence(_("No shared arousal state.")))
        elif kind == "asynchrony_direction":
            sentences.append(
                _sentence(
                    _("The signals moved in opposite directions.")
                    if fragment["subtype"] == OPPOSITE_DIRECTION
                    else _("The signals were unrelated rather than opposed.")
                )
            )
        elif kind == "choice":
            values = {"count": fragment["candidate_count"], "step": fragment["step"], "rank": fragment["rank"]}
            if fragment["candidate_count"] == 1:
                template = _("The only moment meeting STEP %(step)s.")
            elif fragment["rank"] == 1:
                template = ngettext(
                    "Of %(count)s moment meeting STEP %(step)s, this one has %(rule)s.",
                    "Of %(count)s moments meeting STEP %(step)s, this one has %(rule)s.",
                    fragment["candidate_count"],
                )
                values["rule"] = _rule(fragment["rule"])
            else:
                template = ngettext(
                    "Ranked %(rank)s of %(count)s moment meeting STEP %(step)s, ordered %(order)s (the stronger ones "
                    "were selected earlier or overlap a selected moment).",
                    "Ranked %(rank)s of %(count)s moments meeting STEP %(step)s, ordered %(order)s (the stronger ones "
                    "were selected earlier or overlap a selected moment).",
                    fragment["candidate_count"],
                )
                values["order"] = _order(fragment["rule"])
            sentences.append(_sentence(template, **values))
        elif kind == "repeat_pass":
            sentences.append(
                _sentence(
                    _("STEP 6: %(question)s%(target)s → NO → %(instruction)s Selected in pass %(pass)s."),
                    question=quote(fragment["quote"]),
                    target=_project_target(fragment),
                    instruction=quote(fragment["instruction"]),
                    **{"pass": fragment["pass_number"]},
                )
            )
        elif kind == "reserve":
            sentences.append(
                _sentence(
                    _("Reserve moment: %(quote)s%(target)s."),
                    quote=quote(fragment["quote"]),
                    target=_project_target(fragment),
                )
            )
        else:  # pragma: no cover - fragment kinds are a closed set
            raise ValueError(f"Unknown justification fragment: {kind}")
    return sentences
