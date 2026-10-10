import culprit.cli as cli
from culprit.batch import BatchSummary
from culprit.verdict import Verdict, save_verdict


def test_report_writes_and_prints_the_markdown(tmp_path, capsys):
    runs = tmp_path / "runs"
    save_verdict(runs / "a__1", Verdict("a:1", "wrong-result", "harness", "harness passed 4/5 against the baseline's 0/5", {}, {}))
    assert cli.main(["report", "--runs", str(runs)]) == 0
    written = (runs / "report.md").read_text()
    assert "Change harness first" in written
    assert written in capsys.readouterr().out


def test_diagnose_all_prints_the_summary_and_writes_the_report(tmp_path, capsys, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    seen = {}

    def fake_all(runs_dir, projects_dir, **kwargs):
        seen.update(kwargs)
        return BatchSummary(diagnosed=["a:1"], skipped={"b:1": "not restored — run `culprit restore b:1`"},
                            over_cap=["c:1"], stopped_at="d:1", spent_usd=88.5)

    monkeypatch.setattr(cli, "diagnose_all", fake_all)
    assert cli.main(["diagnose-all", "--yes", "--runs", str(runs), "--projects", str(tmp_path / "projects"),
                     "--total", "100"]) == 0
    out = capsys.readouterr().out
    assert seen["total_usd"] == 100.0 and seen["per_episode_usd"] == 40.0 and seen["n"] == 5
    for expected in ["diagnosed 1", "b:1 skipped: not restored", "c:1 hit its $40.00 cap",
                     "stopped at d:1", "$88.50", "rerun"]:
        assert expected in out
    assert (runs / "report.md").exists()
