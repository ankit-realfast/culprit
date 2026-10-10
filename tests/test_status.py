import json

import culprit.cli as cli
from culprit.model import Criterion, save_criterion
from culprit.report import episode_status, next_step, queue_status
from culprit.store import episode_dir, short_id
from culprit.diagnose import Verdict, save_verdict

ID = "aaaaaaaa1111:bbbbbbbb2222"


def queue(runs, *ids):
    runs.mkdir(parents=True, exist_ok=True)
    rows = [{"score": 9 - i, "summary": {"episode_id": e}} for i, e in enumerate(ids)]
    (runs / "candidates.json").write_text(json.dumps(rows))


def confirm(runs, episode_id=ID):
    episode_dir(runs, episode_id).mkdir(parents=True, exist_ok=True)
    (episode_dir(runs, episode_id) / "symptom.json").write_text(
        json.dumps({"episode_id": episode_id, "symptom": "wrong-result"}))


def baseline(runs, passes=1, runs_count=5, cost=1.5):
    folder = episode_dir(runs, ID) / "replays" / "baseline"
    folder.mkdir(parents=True)
    (folder / "outcome.json").write_text(json.dumps({"variant": "baseline", "passes": passes, "runs": runs_count,
                                                     "cost_usd": cost, "void": 0}))
    (folder / "0").mkdir()
    (folder / "0" / "result.json").write_text(json.dumps({"cost_usd": cost}))


def test_each_stage_names_the_first_missing_step(tmp_path):
    runs = tmp_path / "runs"
    queue(runs, ID)
    steps = []

    def record():
        status = episode_status(runs, ID)
        steps.append((status.stage, next_step(runs, ID)))

    record()
    confirm(runs)
    record()
    save_criterion(runs, ID, Criterion("works", (), ()))
    record()
    save_criterion(runs, ID, Criterion("works", (), (), approved=True))
    record()
    (episode_dir(runs, ID) / "pristine").mkdir()
    record()
    baseline(runs)
    record()
    save_verdict(episode_dir(runs, ID), Verdict(ID, "wrong-result", "harness", "harness passed 4/5", {}, {}))
    record()
    stages = [stage for stage, _ in steps]
    assert stages == ["queued", "confirmed", "criterion drafted", "criterion approved", "restored", "baselined",
                      "diagnosed"]
    hints = [hint for _, hint in steps]
    assert f"culprit confirm {short_id(ID)} <symptom>" in hints[0]
    assert f"culprit confirm {short_id(ID)} wrong-result" in hints[1] and "draft" in hints[1]
    assert f"culprit status {short_id(ID)}" in hints[2] and "approve" in hints[2]
    assert f"culprit confirm {short_id(ID)} wrong-result" in hints[2]
    for hint in hints[3:6]:  # diagnose restores and runs the baseline itself
        assert hint == f"run `culprit diagnose {short_id(ID)}`"
    assert "culprit report" in hints[6]


def test_an_episode_outside_the_queue_needs_triage_first(tmp_path):
    runs = tmp_path / "runs"
    queue(runs, "other:one")
    episode_dir(runs, ID).mkdir(parents=True)
    assert episode_status(runs, ID).stage == "not queued"
    assert "culprit triage" in next_step(runs, ID)


def test_queue_status_lists_queued_episodes_and_episode_folders(tmp_path):
    runs = tmp_path / "runs"
    queue(runs, ID, "cccccccc3333:dddddddd4444")
    confirm(runs, "eeeeeeee5555:ffffffff6666")
    assert [(s.episode_id, s.stage) for s in queue_status(runs)] == [
        (ID, "queued"), ("cccccccc3333:dddddddd4444", "queued"), ("eeeeeeee5555:ffffffff6666", "confirmed")]


def test_status_without_an_id_prints_one_line_per_episode(tmp_path, capsys):
    runs = tmp_path / "runs"
    queue(runs, ID)
    confirm(runs)
    assert cli.main(["status"]) == 0
    out, trust = capsys.readouterr().out.split("\n\n")  # the episode rows, then how far to trust verdicts
    out = out.splitlines()
    assert len(out) == 1
    assert "judge: 0 of 0 labels agree" in trust and "selftest: not run" in trust
    assert "aaaaaaaa:bbbbbbbb" in out[0] and "confirmed" in out[0] and f"culprit confirm {short_id(ID)} wrong-result" in out[0]


def test_status_with_an_id_shows_criterion_restore_runs_spend_and_verdict(tmp_path, capsys):
    runs = tmp_path / "runs"
    queue(runs, ID)
    confirm(runs)
    save_criterion(runs, ID, Criterion("the tests pass", ("pytest -q",), ("delete tests",), approved=True))
    (episode_dir(runs, ID) / "pristine").mkdir()
    (episode_dir(runs, ID) / "restore.json").write_text(json.dumps({"snapshot_agreed": 3, "checked": 4,
                                                                    "unknown": 1, "missing_backups": 0}))
    baseline(runs)
    save_verdict(episode_dir(runs, ID), Verdict(ID, "wrong-result", "harness", "harness passed 4/5", {}, {}))
    assert cli.main(["status", "#1"]) == 0
    out = capsys.readouterr().out
    assert "diagnosed" in out
    assert "approved" in out and "the tests pass" in out
    assert "check: pytest -q" in out and "must not: delete tests" in out
    assert "3 of 4" in out
    assert "baseline: 1 of 5 passed" in out
    assert "$1.50" in out
    assert "cause: harness" in out


def test_status_refuses_a_missing_store(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CULPRIT_RUNS", str(tmp_path / "nowhere"))
    assert cli.main(["status"]) == 3
    assert "no culprit store" in capsys.readouterr().err
