import json

import pytest

from culprit.criterion import Criterion
from culprit.episodes import episodes_from_session
from culprit.experiment import BudgetExhausted, EpisodeBudget, VariantOutcome, load_outcome, run_variant
from culprit.judge import Judgement
from culprit.replay import ReplayResult, ReplaySettings
from culprit.store import episode_dir
from records import human

CRITERION = Criterion("works", (), (), approved=True)
SETTINGS = ReplaySettings(prompt="fix", model="claude-opus-5", permission_mode="auto", max_budget_usd=1.0)


def episode_at(write_session, tmp_path):
    (episode,) = episodes_from_session(write_session([human("fix", "p1", session_id="s1", cwd=str(tmp_path / "proj"))]))
    return episode


def fake_replay(cost=0.25):
    calls = []

    def replay(settings, pristine, run_dir, workdir, checks, **kwargs):
        calls.append((settings, pristine, run_dir, workdir, checks))
        run_dir.mkdir(parents=True, exist_ok=True)
        return ReplayResult(run_dir, 0, False, "done", cost, 3, ())

    replay.calls = calls
    return replay


def judge_sequence(*passed):
    answers = list(passed)
    return lambda criterion, result: Judgement(answers.pop(0), "r", "model")


def test_a_variant_runs_n_times_and_stores_each_judgement_and_the_outcome(write_session, tmp_path):
    episode = episode_at(write_session, tmp_path)
    runs = tmp_path / "runs"
    replay = fake_replay()
    outcome = run_variant("baseline", SETTINGS, n=3, episode=episode, runs_dir=runs, criterion=CRITERION,
                          budget=EpisodeBudget(10.0), replay=replay, judge=judge_sequence(True, False, True))
    assert outcome == VariantOutcome("baseline", passes=2, runs=3, cost_usd=0.75)
    folder = episode_dir(runs, episode.episode_id)
    assert json.loads((folder / "replays" / "baseline" / "1" / "judgement.json").read_text())["passed"] is False
    assert load_outcome(folder, "baseline") == outcome
    assert all(call[1] == folder / "pristine" for call in replay.calls)
    assert all(call[3].parent == tmp_path and call[3].name.startswith(".culprit-replay-") for call in replay.calls)


def test_the_budget_stops_before_the_run_that_would_cross_it(write_session, tmp_path):
    episode = episode_at(write_session, tmp_path)
    budget = EpisodeBudget(cap_usd=3.2)
    replay = fake_replay(cost=1.0)
    with pytest.raises(BudgetExhausted, match="3.20"):
        run_variant("baseline", SETTINGS, n=5, episode=episode, runs_dir=tmp_path / "runs", criterion=CRITERION,
                    budget=budget, replay=replay, judge=judge_sequence(*[True] * 5))
    # each run reserves its $1.00 cap plus the $0.50 judge cap: two runs fit in $3.20, a third would not
    assert len(replay.calls) == 2


def test_out_dir_keeps_runs_apart_from_the_episode_folder(write_session, tmp_path):
    episode = episode_at(write_session, tmp_path)
    out = tmp_path / "selftest" / "weak-model"
    run_variant("baseline", SETTINGS, n=1, episode=episode, runs_dir=tmp_path / "runs", criterion=CRITERION,
                budget=EpisodeBudget(10.0), replay=fake_replay(), judge=judge_sequence(True), out_dir=out)
    assert load_outcome(out, "baseline").passes == 1
    assert not (episode_dir(tmp_path / "runs", episode.episode_id) / "replays").exists()


def test_a_variant_never_run_loads_as_none(tmp_path):
    assert load_outcome(tmp_path, "model") is None


def test_void_runs_are_retried_and_left_out_of_the_count(write_session, tmp_path):
    episode = episode_at(write_session, tmp_path)
    verdicts = [Judgement(False, "x", "void"), Judgement(True, "r", "model"), Judgement(False, "x", "void"),
                Judgement(False, "r", "model")]
    replay = fake_replay(cost=0.1)
    outcome = run_variant("baseline", SETTINGS, n=2, episode=episode, runs_dir=tmp_path / "runs", criterion=CRITERION,
                          budget=EpisodeBudget(10.0), replay=replay, judge=lambda c, r: verdicts.pop(0))
    assert (outcome.passes, outcome.runs, outcome.void) == (1, 2, 2)
    assert len(replay.calls) == 4
