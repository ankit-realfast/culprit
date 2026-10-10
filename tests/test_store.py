import json

import pytest

from culprit.signals import FailureSignals
from culprit.store import UnknownEpisode, episode_dir, read_symptom, record_symptom, save_candidates
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
