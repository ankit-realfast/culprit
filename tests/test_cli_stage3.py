import culprit.cli as cli
from culprit.criterion import Criterion, save_criterion
from culprit.experiment import VariantOutcome
from culprit.store import episode_dir
from records import human, snapshot


def setup_episode(write_session, tmp_path, approved=True, restored=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([snapshot("u-p1", {}), human("fix a", "p1", session_id="s1", cwd=str(repo))], name="s1.jsonl")
    runs = tmp_path / "runs"
    save_criterion(runs, "s1:p1", Criterion("works", (), (), approved=approved))
    if restored:
        (episode_dir(runs, "s1:p1") / "pristine").mkdir(parents=True)
    return tmp_path / "projects", runs


def test_baseline_runs_the_unchanged_episode_and_prints_the_pass_rate(write_session, tmp_path, capsys, monkeypatch):
    projects, runs = setup_episode(write_session, tmp_path)
    seen = {}

    def fake_run_variant(variant, settings, **kwargs):
        seen.update(variant=variant, settings=settings, n=kwargs["n"], cap=kwargs["budget"].cap_usd)
        return VariantOutcome(variant, 1, kwargs["n"], 3.1)

    monkeypatch.setattr(cli, "run_variant", fake_run_variant)
    exit_code = cli.main(["baseline", "s1:p1", "--projects", str(projects), "--runs", str(runs), "--n", "4",
                          "--budget", "12"])
    assert exit_code == 0
    assert seen["variant"] == "baseline" and seen["n"] == 4 and seen["cap"] == 12.0
    assert seen["settings"].prompt == "fix a"
    assert "baseline: 1 of 4 passed" in capsys.readouterr().out


def test_baseline_names_the_missing_step(write_session, tmp_path, capsys):
    projects, runs = setup_episode(write_session, tmp_path, approved=False)
    assert cli.main(["baseline", "s1:p1", "--projects", str(projects), "--runs", str(runs)]) == 1
    assert "culprit criterion approve" in capsys.readouterr().err


def test_baseline_before_restore_names_restore(write_session, tmp_path, capsys):
    projects, runs = setup_episode(write_session, tmp_path, restored=False)
    assert cli.main(["baseline", "s1:p1", "--projects", str(projects), "--runs", str(runs)]) == 1
    assert "culprit restore" in capsys.readouterr().err


def test_baseline_counts_what_the_episode_already_spent(write_session, tmp_path, monkeypatch):
    import json
    projects, runs = setup_episode(write_session, tmp_path)
    old = episode_dir(runs, "s1:p1") / "replays" / "baseline" / "0"
    old.mkdir(parents=True)
    (old / "result.json").write_text(json.dumps({"cost_usd": 3.0}))
    seen = {}

    def fake_run_variant(variant, settings, **kwargs):
        seen["spent"] = kwargs["budget"].spent_usd
        return VariantOutcome(variant, 0, 1, 0.0)

    monkeypatch.setattr(cli, "run_variant", fake_run_variant)
    cli.main(["baseline", "s1:p1", "--projects", str(projects), "--runs", str(runs)])
    assert seen["spent"] == 3.0
