import json

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
def isolated_from_real_data(tmp_path, monkeypatch):
    """No test may read the real session logs or file history or write the repo's runs/: the store is
    tmp_path/runs (CULPRIT_RUNS), the logs tmp_path/projects (CULPRIT_PROJECTS, where write_session writes),
    and Claude Code's file history tmp_path/file-history."""
    import culprit.cli as cli
    import culprit.replay as replay
    import culprit.sandbox as sandbox

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("CULPRIT_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("CULPRIT_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.setattr(cli, "DEFAULT_PROJECTS", tmp_path / "no-real-projects")
    monkeypatch.setattr(replay, "DEFAULT_PROJECTS", tmp_path / "no-real-projects")
    monkeypatch.setattr(sandbox, "DEFAULT_FILE_HISTORY", tmp_path / "file-history")
