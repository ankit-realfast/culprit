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
