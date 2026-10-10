from pathlib import Path

import culprit.cli as cli
import culprit.replay as replay

REPO = Path(__file__).resolve().parent.parent


def test_a_command_without_store_settings_never_reaches_the_repo_or_real_logs(write_session, monkeypatch):
    before = (REPO / "runs" / "candidates.json").read_bytes() if (REPO / "runs" / "candidates.json").exists() else None
    cli.main(["triage"])
    monkeypatch.delenv("CULPRIT_RUNS")
    monkeypatch.delenv("CULPRIT_PROJECTS")
    cli.main(["triage"])
    after = (REPO / "runs" / "candidates.json").read_bytes() if (REPO / "runs" / "candidates.json").exists() else None
    assert before == after
    assert Path.cwd() != REPO
    real = Path.home() / ".claude"
    assert cli.default_projects() != real / "projects"
    assert replay.DEFAULT_FILE_HISTORY != real / "file-history"
