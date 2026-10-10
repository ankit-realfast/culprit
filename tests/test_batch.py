import json

from culprit.batch import diagnose_all, readiness_problem
from culprit.criterion import Criterion, save_criterion
from culprit.experiment import BudgetExhausted
from culprit.store import episode_dir
from culprit.verdict import Verdict, save_verdict


class FakeEpisode:
    def __init__(self, episode_id):
        self.episode_id = episode_id
        self.prompt_text = "fix"
        self.model = "claude-opus-5"
        self.permission_mode = "auto"
        self.cwd = "/repo"


def queue(runs, *episode_ids):
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "candidates.json").write_text(json.dumps(
        [{"summary": {"episode_id": e, "session_file": "/s.jsonl", "prompt_excerpt": "x", "signals": {}}, "score": 5}
         for e in episode_ids]))


def prepare(runs, episode_id, symptom="wrong-result", approved=True, restored=True):
    folder = episode_dir(runs, episode_id)
    folder.mkdir(parents=True, exist_ok=True)
    if symptom:
        (folder / "symptom.json").write_text(json.dumps({"episode_id": episode_id, "symptom": symptom}))
    save_criterion(runs, episode_id, Criterion("works", (), (), approved=approved))
    if restored:
        (folder / "pristine").mkdir(exist_ok=True)


def spending_diagnose(costs, raises=()):
    calls = []

    def fake(episode, criterion, symptom, base, **kwargs):
        calls.append((episode.episode_id, kwargs["budget"].cap_usd))
        kwargs["budget"].spend(costs.get(episode.episode_id, 0.0))
        if episode.episode_id in raises:
            raise BudgetExhausted("over")
        verdict = Verdict(episode.episode_id, symptom.value, "harness", "e", {}, {})
        save_verdict(episode_dir(kwargs["runs_dir"], episode.episode_id), verdict)
        return verdict

    fake.calls = calls
    return fake


def run(runs, fake, total=400.0, per=40.0):
    return diagnose_all(runs, runs / "projects", n=5, per_episode_usd=per, total_usd=total, replay_budget_usd=2.0,
                        diagnose_fn=fake, find=lambda projects, episode_id: FakeEpisode(episode_id),
                        follow=lambda projects, episode_id: [])


def test_readiness_names_the_first_missing_step(tmp_path):
    prepare(tmp_path, "a:1", symptom=None)
    prepare(tmp_path, "b:1", approved=False)
    prepare(tmp_path, "c:1", restored=False)
    prepare(tmp_path, "d:1")
    assert "culprit confirm" in readiness_problem(tmp_path, "a:1")
    assert "culprit criterion approve" in readiness_problem(tmp_path, "b:1")
    assert "culprit restore" in readiness_problem(tmp_path, "c:1")
    assert readiness_problem(tmp_path, "d:1") is None


def test_ready_episodes_are_diagnosed_and_the_rest_skipped_with_reasons(tmp_path):
    runs = tmp_path / "runs"
    queue(runs, "a:1", "b:1", "c:1")
    prepare(runs, "a:1")
    prepare(runs, "b:1", restored=False)
    prepare(runs, "c:1")
    save_verdict(episode_dir(runs, "c:1"), Verdict("c:1", "wrong-result", "task", "e", {}, {}))
    summary = run(runs, spending_diagnose({"a:1": 12.5}))
    assert summary.diagnosed == ["a:1"]
    assert "culprit restore" in summary.skipped["b:1"]
    assert summary.skipped["c:1"] == "already diagnosed"
    assert summary.spent_usd == 12.5


def test_one_episode_over_its_cap_does_not_stop_the_others(tmp_path):
    runs = tmp_path / "runs"
    queue(runs, "a:1", "b:1")
    prepare(runs, "a:1")
    prepare(runs, "b:1")
    summary = run(runs, spending_diagnose({"a:1": 40.0, "b:1": 10.0}, raises=("a:1",)))
    assert summary.over_cap == ["a:1"] and summary.diagnosed == ["b:1"]
    assert summary.stopped_at is None
    assert summary.spent_usd == 50.0


def test_the_total_budget_stops_the_batch_and_names_where(tmp_path):
    runs = tmp_path / "runs"
    queue(runs, "a:1", "b:1", "c:1")
    for episode_id in ("a:1", "b:1", "c:1"):
        prepare(runs, episode_id)
    fake = spending_diagnose({"a:1": 30.0, "b:1": 30.0})
    summary = run(runs, fake, total=60.0, per=40.0)
    assert [episode for episode, _ in fake.calls] == ["a:1", "b:1"]
    assert fake.calls[1][1] == 30.0  # the second episode only got what was left of the total
    assert summary.stopped_at == "c:1"
    assert summary.diagnosed == ["a:1", "b:1"]


def test_a_total_that_runs_out_inside_an_episode_is_reported_as_the_stop(tmp_path):
    runs = tmp_path / "runs"
    queue(runs, "a:1", "b:1")
    prepare(runs, "a:1")
    prepare(runs, "b:1")
    summary = run(runs, spending_diagnose({"a:1": 9.0}, raises=("a:1",)), total=11.0)
    assert summary.stopped_at == "a:1"
    assert summary.over_cap == []


def test_the_total_counts_what_earlier_batches_spent(tmp_path):
    runs = tmp_path / "runs"
    queue(runs, "a:1")
    prepare(runs, "a:1")
    old = runs / "z__9" / "replays" / "baseline" / "0"
    old.mkdir(parents=True)
    (old / "result.json").write_text(json.dumps({"cost_usd": 399.0}))
    fake = spending_diagnose({})
    summary = run(runs, fake, total=400.0)
    assert summary.stopped_at == "a:1" and fake.calls == []


def test_one_failing_episode_does_not_end_the_batch(tmp_path):
    from culprit.claude import ClaudeCallFailed
    runs = tmp_path / "runs"
    queue(runs, "a:1", "b:1")
    prepare(runs, "a:1")
    prepare(runs, "b:1")
    base = spending_diagnose({})

    def flaky(episode, *args, **kwargs):
        if episode.episode_id == "a:1":
            raise ClaudeCallFailed("judge overloaded")
        return base(episode, *args, **kwargs)

    summary = run(runs, flaky)
    assert "judge overloaded" in summary.skipped["a:1"]
    assert summary.diagnosed == ["b:1"]
