import pytest

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
    exit_code = cli.main(["baseline", "s1:p1"])
    assert exit_code == 0
    assert seen["variant"] == "baseline" and seen["n"] == cli.RUNS_PER_VARIANT and seen["cap"] == 40.0
    assert seen["settings"].prompt == "fix a"
    assert f"baseline: 1 of {cli.RUNS_PER_VARIANT} passed" in capsys.readouterr().out


def test_baseline_names_the_missing_step(write_session, tmp_path, capsys):
    projects, runs = setup_episode(write_session, tmp_path, approved=False)
    assert cli.main(["baseline", "s1:p1"]) == 3
    err = capsys.readouterr().err
    assert "culprit criterion approve" in err and "criterion draft" not in err


def test_baseline_before_restore_names_restore(write_session, tmp_path, capsys):
    projects, runs = setup_episode(write_session, tmp_path, restored=False)
    assert cli.main(["baseline", "s1:p1"]) == 3
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
    cli.main(["baseline", "s1:p1"])
    assert seen["spent"] == 3.0


def test_baseline_over_budget_exits_4(write_session, tmp_path, capsys, monkeypatch):
    from culprit.experiment import BudgetExhausted
    projects, runs = setup_episode(write_session, tmp_path)

    def over(*args, **kwargs):
        raise BudgetExhausted("the next run could spend $3.20")

    monkeypatch.setattr(cli, "run_variant", over)
    assert cli.main(["baseline", "s1:p1"]) == 4
    assert "$3.20" in capsys.readouterr().err


def test_baseline_prints_a_line_on_stderr_for_each_finished_replay(write_session, tmp_path, capsys, monkeypatch):
    from culprit.experiment import ReplayEvent
    projects, runs = setup_episode(write_session, tmp_path)

    def fake_run_variant(variant, settings, **kwargs):
        progress = kwargs["progress"]
        progress(ReplayEvent("start", variant, attempt=0, index=1, n=2, spent_usd=0.0))
        progress(ReplayEvent("finish", variant, attempt=0, index=1, n=2, result="pass", cost_usd=0.84,
                             spent_usd=1.34))
        progress(ReplayEvent("finish", variant, attempt=1, index=2, n=2, result="void", cost_usd=2.0,
                             spent_usd=3.84))
        return VariantOutcome(variant, 1, 1, 2.84)

    monkeypatch.setattr(cli, "run_variant", fake_run_variant)
    cli.main(["baseline", "s1:p1"])
    err = capsys.readouterr().err.splitlines()
    assert "baseline #1 of 2: pass, $0.84 (episode spent $1.34)" in err
    assert "baseline #2 of 2: void, not counted, $2.00 (episode spent $3.84)" in err


@pytest.mark.parametrize("argv", [["baseline", "s1:p1", "--n", "1"], ["diagnose", "s1:p1", "--n", "1"],
                                  ["selftest", "s1:p1", "--n", "1"], ["diagnose-all", "--n", "1"]])
def test_the_run_count_is_not_a_flag(argv, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)
    assert stopped.value.code == 2
