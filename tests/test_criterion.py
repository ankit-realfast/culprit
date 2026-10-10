import json

import pytest

from culprit.criterion import (Criterion, approve_criterion, draft_criterion, drafting_prompt,
                               load_criterion, save_criterion)
from culprit.episodes import episodes_from_session
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
    assert criterion == Criterion(DRAFT["success_means"], ("pytest -q",), ("delete tests",), approved=False)
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
    with pytest.raises(FileNotFoundError, match="culprit criterion draft"):
        approve_criterion(tmp_path, "s1:p1")


def test_an_undrafted_episode_loads_as_none(tmp_path):
    assert load_criterion(tmp_path, "s1:p1") is None
