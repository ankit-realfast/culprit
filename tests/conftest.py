import json
import os

import pytest


@pytest.fixture
def write_session(tmp_path):
    """Write records as a session log under tmp_path/projects/<project>/<name>; return its path."""

    def write(records, name="s1.jsonl", project="-repo", trailing=""):
        project_dir = tmp_path / "projects" / project
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / name
        path.write_text("".join(json.dumps(r) + "\n" for r in records) + trailing)
        return path

    return write


@pytest.fixture(autouse=True)
def no_real_claude(tmp_path, monkeypatch):
    """Tests inject fakes; if one ever reaches the real `claude` binary, a stand-in first on PATH answers
    instead, fails the call, and the test fails, so a missed fake can never spend money."""
    stand_in = tmp_path / "no-real-claude"
    stand_in.mkdir()
    called = tmp_path / "real-claude-called"
    script = stand_in / "claude"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> '{called}'\necho 'tests never run the real claude' >&2\nexit 1\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stand_in}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("CULPRIT_CLAUDE_BIN", raising=False)
    for name in ("CULPRIT_ORACLE_MODEL", "CULPRIT_DRAFTING_MODEL", "CULPRIT_WEAK_MODEL"):
        monkeypatch.delenv(name, raising=False)  # every test sees culprit's own model choices
    yield
    assert not called.exists(), "a test ran the real claude binary instead of a fake"


@pytest.fixture(autouse=True)
def isolated_from_real_data(tmp_path, monkeypatch):
    """No test may read the real session logs or file history or write the repo's runs/: the store is
    tmp_path/runs (CULPRIT_RUNS), the logs tmp_path/projects (CULPRIT_PROJECTS, where write_session writes),
    Claude Code's file history tmp_path/file-history,
    and the CLAUDE.md files outside a project only those under tmp_path."""
    import culprit.cli as cli
    import culprit.diagnose as diagnose
    import culprit.logs as logs
    import culprit.replay as replay

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("CULPRIT_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("CULPRIT_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.setattr(logs, "DEFAULT_PROJECTS", tmp_path / "no-real-projects")
    monkeypatch.setattr(replay, "DEFAULT_FILE_HISTORY", tmp_path / "file-history")
    # the standing-instructions drafter reads CLAUDE.md files: never the real user's, policy's or ancestors'
    monkeypatch.setattr(diagnose, "USER_CLAUDE_DIR", tmp_path / "no-real-home" / ".claude")
    monkeypatch.setattr(diagnose, "MANAGED_INSTRUCTIONS", (tmp_path / "no-real-policy" / "CLAUDE.md",))
    monkeypatch.setattr(diagnose, "MANAGED_SETTINGS", (tmp_path / "no-real-policy" / "managed-settings.json",))
    monkeypatch.setattr(diagnose, "ANCESTOR_ROOT", tmp_path)
    # the CLI drafts a criterion on confirm; tests that care about the draft install their own FakeAsk
    monkeypatch.setattr(cli, "ask_claude", lambda prompt, schema: {"success_means": "works", "checks": [],
                                                                   "must_not": []})
