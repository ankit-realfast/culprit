import culprit.cli as cli
from culprit.diagnose import BatchSummary, Verdict, save_verdict


def test_report_writes_and_prints_the_markdown(tmp_path, capsys):
    runs = tmp_path / "runs"
    save_verdict(runs / "a__1", Verdict("a:1", "wrong-result", "harness", "harness passed 4/5 against the baseline's 0/5", {}, {}))
    assert cli.main(["report"]) == 0
    written = (runs / "report.md").read_text()
    assert "Change harness first" in written
    assert written in capsys.readouterr().out


def test_diagnose_without_an_id_prints_the_summary_and_writes_the_report(tmp_path, capsys, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "candidates.json").write_text("[]")
    seen = {}
    save_verdict(runs / "a__1", Verdict("a:1", "wrong-result", "harness", "e",
                                        {"baseline": {"variant": "baseline", "passes": 0, "runs": 5, "cost_usd": 1.0},
                                         "harness": {"variant": "harness", "passes": 4, "runs": 5, "cost_usd": 1.0}},
                                        {}))

    def fake_all(runs_dir, projects_dir, **kwargs):
        seen.update(kwargs)
        return BatchSummary(diagnosed=["a:1"], skipped={"b:1": "not restored — `culprit diagnose b:1` restores it"},
                            over_cap={"c:1": 3600.0}, stopped_at="d:1", spent_usd=88.5)

    monkeypatch.setattr(cli, "diagnose_all", fake_all)
    assert cli.main(["diagnose"]) == 0
    out = capsys.readouterr().out
    assert seen["total_usd"] == cli.TOTAL_CAP_USD and seen["per_episode_usd"] == cli.episode_cap_usd and seen["n"] == cli.RUNS_PER_VARIANT
    for expected in ["diagnosed 1", "b:1 skipped: not restored", "c:1 hit its $3600.00 cap",
                     "stopped at d:1", "$88.50", "rerun",
                     "a:1 harness: baseline 0/5 → 4/5, p = 0.0238"]:
        assert expected in out
    assert "--" not in out  # no flag left to suggest
    assert (runs / "report.md").exists()
