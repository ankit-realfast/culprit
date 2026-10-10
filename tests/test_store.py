import json

import pytest

from culprit.signals import FailureSignals
from culprit.store import (NotReady, UnknownEpisode, episode_dir, read_symptom, record_symptom, resolve_episode_id,
                           save_candidates, short_id)
from culprit.taxonomy import Symptom
from culprit.triage import Candidate, EpisodeSummary


def saved(runs_dir, *episode_ids):
    candidates = [Candidate(EpisodeSummary(i, "/s.jsonl", "prompt", FailureSignals(True, 0, 0, 0, 1.0)), 5)
                  for i in episode_ids]
    return save_candidates(runs_dir, candidates)


def test_symptoms_are_the_spec_values_in_order():
    assert [s.value for s in Symptom] == [
        "stopped-short", "wrong-result", "ignored-instructions", "wasteful", "no-failure"]


def test_saved_candidates_are_readable_json(tmp_path):
    path = saved(tmp_path, "abc:p1")
    (row,) = json.loads(path.read_text())
    assert row["summary"]["episode_id"] == "abc:p1"
    assert row["summary"]["signals"]["interrupted"] is True
    assert row["score"] == 5


def test_a_recorded_symptom_reads_back_from_the_episode_folder(tmp_path):
    saved(tmp_path, "abc:p1")
    path = record_symptom(tmp_path, "abc:p1", Symptom.WRONG_RESULT)
    assert path.parent == episode_dir(tmp_path, "abc:p1")
    assert ":" not in path.parent.name
    assert read_symptom(tmp_path, "abc:p1") is Symptom.WRONG_RESULT


def test_a_second_label_replaces_the_first(tmp_path):
    saved(tmp_path, "abc:p1")
    record_symptom(tmp_path, "abc:p1", Symptom.WASTEFUL)
    record_symptom(tmp_path, "abc:p1", Symptom.NO_FAILURE)
    assert read_symptom(tmp_path, "abc:p1") is Symptom.NO_FAILURE


def test_recording_an_unknown_episode_is_refused_and_writes_nothing(tmp_path):
    saved(tmp_path, "abc:p1")
    with pytest.raises(UnknownEpisode, match="abc:p2"):
        record_symptom(tmp_path, "abc:p2", Symptom.WASTEFUL)
    assert not episode_dir(tmp_path, "abc:p2").exists()


def test_recording_before_triage_names_the_missing_step(tmp_path):
    with pytest.raises(FileNotFoundError, match="culprit triage"):
        record_symptom(tmp_path, "abc:p1", Symptom.WASTEFUL)


def test_an_unlabelled_episode_reads_as_none(tmp_path):
    assert read_symptom(tmp_path, "abc:p1") is None


def test_a_new_triage_keeps_queued_episodes_and_takes_the_new_score(tmp_path):
    saved(tmp_path, "abc:p1", "abc:p2")
    path = save_candidates(tmp_path, [
        Candidate(EpisodeSummary("abc:p2", "/s.jsonl", "prompt", FailureSignals(True, 3, 0, 0, 1.0)), 8),
        Candidate(EpisodeSummary("abc:p3", "/s.jsonl", "prompt", FailureSignals(True, 0, 0, 0, 1.0)), 1)])
    rows = json.loads(path.read_text())
    assert [(r["summary"]["episode_id"], r["score"]) for r in rows] == [("abc:p2", 8), ("abc:p1", 5), ("abc:p3", 1)]


def test_resolve_takes_a_queue_row_number(tmp_path):
    saved(tmp_path, "aaaa1111-x:p1", "bbbb2222-y:p2")
    assert resolve_episode_id(tmp_path, "#2") == "bbbb2222-y:p2"
    with pytest.raises(UnknownEpisode, match="#3"):
        resolve_episode_id(tmp_path, "#3")


def test_resolve_a_row_number_before_triage_is_not_ready(tmp_path):
    with pytest.raises(NotReady, match="culprit triage"):
        resolve_episode_id(tmp_path, "#1")


def test_resolve_takes_a_unique_prefix_from_the_queue_or_a_folder(tmp_path):
    saved(tmp_path, "aaaa1111-x:p1")
    (tmp_path / "cccc3333-z__p9").mkdir()
    assert resolve_episode_id(tmp_path, "aaaa") == "aaaa1111-x:p1"
    assert resolve_episode_id(tmp_path, "cccc") == "cccc3333-z:p9"


def test_resolve_lists_every_match_when_a_prefix_is_ambiguous(tmp_path):
    saved(tmp_path, "aaaa1111-x:p1", "aaaa1111-x:p2")
    with pytest.raises(UnknownEpisode, match="aaaa1111-x:p1.*aaaa1111-x:p2"):
        resolve_episode_id(tmp_path, "aaaa")


def test_resolve_refuses_text_that_matches_nothing(tmp_path):
    with pytest.raises(UnknownEpisode, match="no episode in .* matches zzz"):
        resolve_episode_id(tmp_path, "zzz")


def test_resolve_passes_a_full_id_through_and_expands_a_short_one(tmp_path):
    saved(tmp_path, "aaaa1111-session:pppp2222-prompt")
    assert resolve_episode_id(tmp_path, "not:queued") == "not:queued"
    assert resolve_episode_id(tmp_path, "aaaa1111-session:pppp2222-prompt") == "aaaa1111-session:pppp2222-prompt"
    assert resolve_episode_id(tmp_path, "aaaa1111:pppp2222") == "aaaa1111-session:pppp2222-prompt"
    assert short_id("aaaa1111-session:pppp2222-prompt") == "aaaa1111:pppp2222"
