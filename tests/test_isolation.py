from pathlib import Path

import culprit.cli as cli

REPO = Path(__file__).resolve().parent.parent


def test_a_command_without_store_flags_never_reaches_the_repo_or_real_logs(write_session):
    before = (REPO / "runs" / "candidates.json").read_bytes() if (REPO / "runs" / "candidates.json").exists() else None
    cli.main(["triage"])
    after = (REPO / "runs" / "candidates.json").read_bytes() if (REPO / "runs" / "candidates.json").exists() else None
    assert before == after
    assert Path.cwd() != REPO
    assert Path.home() / ".claude" / "projects" != cli.DEFAULT_PROJECTS
