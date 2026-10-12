import json
from dataclasses import replace

import pytest

from culprit.logs import episodes_from_session
from culprit.model import Criterion, approve_criterion, draft_criterion, drafting_prompt, load_criterion, save_criterion
from fakes import FakeAsk
from records import human

DRAFT = {"success_means": "pytest passes and the login bug no longer reproduces",
         "checks": ["pytest -q"], "must_not": ["delete tests"]}


def an_episode(write_session):
    (episode,) = episodes_from_session(write_session([human("fix the login bug", "p1")]))
    return episode


def test_the_drafting_prompt_carries_the_request_and_the_reaction(write_session):
    prompt = drafting_prompt(an_episode(write_session), ["no, it still fails on empty passwords"])
    assert "fix the login bug" in prompt
    assert "still fails on empty passwords" in prompt
    assert "never the implementation" in prompt


def test_a_draft_is_unapproved_and_built_from_the_answer(write_session):
    ask = FakeAsk(DRAFT)
    criterion = draft_criterion(an_episode(write_session), ["it still fails"], ask=ask)
    assert replace(criterion, drafted_hash=None) == Criterion(DRAFT["success_means"], ("pytest -q",),
                                                              ("delete tests",), approved=False)
    assert ask.calls[0][1]["required"] == ["success_means", "checks", "must_not"]


def test_save_load_and_approve_round_trip(tmp_path):
    save_criterion(tmp_path, "s1:p1", Criterion("works", ("make test",), ()))
    assert load_criterion(tmp_path, "s1:p1").approved is False
    approved = approve_criterion(tmp_path, "s1:p1")
    assert approved.approved is True
    assert load_criterion(tmp_path, "s1:p1") == approved


def test_approve_reads_the_human_edited_file(tmp_path):
    path = save_criterion(tmp_path, "s1:p1", Criterion("works", (), ()))
    edited = json.loads(path.read_text())
    edited["success_means"] = "the CSV export has a header row"
    path.write_text(json.dumps(edited))
    assert approve_criterion(tmp_path, "s1:p1").success_means == "the CSV export has a header row"


def test_approving_without_a_draft_names_the_missing_step(tmp_path):
    with pytest.raises(FileNotFoundError, match="culprit confirm s1:p1 <symptom>"):
        approve_criterion(tmp_path, "s1:p1")


def test_an_undrafted_episode_loads_as_none(tmp_path):
    assert load_criterion(tmp_path, "s1:p1") is None


# --- symptom-aware drafting ---

from culprit.model import Limits, criterion_schema, original_effort  # noqa: E402
from culprit.store import Symptom  # noqa: E402
from records import system, tool_result, tool_use  # noqa: E402

WASTEFUL_DRAFT = {**DRAFT, "limits": {"max_tool_calls": 15, "max_minutes": 4},
                  "limits_reason": "the original took 30 tool calls and 8 minutes; half is plenty"}


def a_long_episode(write_session, calls=30, minutes=8):
    records = [human("fix the login bug", "p1")]
    for _ in range(calls):
        records += [tool_use("Bash", command="pytest -q"), tool_result("p1")]
    records.append(system("turn_duration", durationMs=minutes * 60_000))
    (episode,) = episodes_from_session(write_session(records))
    return episode


def test_the_original_effort_counts_tool_calls_and_minutes(write_session):
    effort = original_effort(a_long_episode(write_session))
    assert (effort.tool_calls, effort.minutes, effort.usd) == (30, 8.0, None)


def test_the_drafter_is_told_the_symptom_and_what_happened(write_session):
    prompt = drafting_prompt(a_long_episode(write_session), ["it works but took ages"], Symptom.WASTEFUL)
    assert "wasteful" in prompt and "took far too long" in prompt
    assert "30 tool calls" in prompt and "8 minutes" in prompt and "cost unknown" in prompt
    assert "max_tool_calls" in prompt and "about half" in prompt
    assert "never the implementation" in prompt


@pytest.mark.parametrize("symptom, expected", [
    (Symptom.STOPPED_SHORT, "every part of the request"),
    (Symptom.IGNORED_INSTRUCTIONS, "each instruction the agent ignored"),
    (Symptom.WRONG_RESULT, "the correct outcome"),
    (Symptom.NO_FAILURE, "the outcome"),
])
def test_each_symptom_shapes_the_draft(write_session, symptom, expected):
    prompt = drafting_prompt(an_episode(write_session), [], symptom)
    assert symptom.value in prompt and expected in prompt
    assert "about half" not in prompt


def test_only_a_wasteful_draft_must_set_limits():
    assert {"limits", "limits_reason"} <= set(criterion_schema(Symptom.WASTEFUL)["required"])
    assert criterion_schema(Symptom.WASTEFUL)["properties"]["limits"]["required"] == ["max_tool_calls", "max_minutes"]
    assert "limits" not in criterion_schema(Symptom.WRONG_RESULT)["required"]
    assert "limits" in criterion_schema(Symptom.WRONG_RESULT)["properties"]


def test_a_wasteful_draft_keeps_its_limits_and_the_symptom_it_was_written_for(write_session, tmp_path):
    ask = FakeAsk(WASTEFUL_DRAFT)
    criterion = draft_criterion(a_long_episode(write_session), [], Symptom.WASTEFUL, ask=ask)
    assert criterion.limits == Limits(max_tool_calls=15, max_minutes=4)
    assert criterion.limits_reason.startswith("the original took 30")
    assert criterion.symptom == "wasteful" and criterion.drafted_hash
    save_criterion(tmp_path / "runs", "s1:p1", criterion)
    assert load_criterion(tmp_path / "runs", "s1:p1") == criterion


def test_a_wasteful_draft_without_limits_falls_back_to_half_the_original(write_session):
    criterion = draft_criterion(a_long_episode(write_session), [], Symptom.WASTEFUL, ask=FakeAsk(DRAFT))
    assert criterion.limits == Limits(max_tool_calls=15, max_minutes=4)
    assert "half" in criterion.limits_reason


def test_limits_a_draft_sets_for_another_symptom_are_kept_and_empty_ones_dropped(write_session):
    with_limits = draft_criterion(an_episode(write_session), [], Symptom.WRONG_RESULT,
                                  ask=FakeAsk({**DRAFT, "limits": {"max_tool_calls": 40}}))
    assert with_limits.limits == Limits(max_tool_calls=40)
    empty = draft_criterion(an_episode(write_session), [], Symptom.WRONG_RESULT,
                            ask=FakeAsk({**DRAFT, "limits": {}}))
    assert empty.limits is None


def test_a_criterion_written_before_limits_still_loads(tmp_path):
    from culprit.store import episode_dir
    folder = episode_dir(tmp_path, "s1:p1")
    folder.mkdir(parents=True)
    (folder / "criterion.json").write_text(json.dumps({"success_means": "works", "checks": [], "must_not": [],
                                                       "approved": False, "model": None}))
    loaded = load_criterion(tmp_path, "s1:p1")
    assert (loaded.limits, loaded.limits_reason, loaded.symptom, loaded.drafted_hash) == (None, "", None, None)
