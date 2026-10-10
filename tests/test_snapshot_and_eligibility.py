import json
from pathlib import Path

from culprit.eligibility import project_hooks, replay_blockers
from culprit.episodes import episodes_from_session
from culprit.snapshot import load_snapshot, session_root
from records import human, snapshot, tool_use


def only_episode(write_session, records):
    (episode,) = episodes_from_session(write_session(records))
    return episode


def test_the_snapshot_keyed_by_the_prompt_uuid_is_loaded_and_the_last_one_wins(write_session):
    episode = only_episode(write_session, [
        snapshot("u-p1", {"a.py": "abc@v1"}),
        human("x", "p1"),
        snapshot("u-other", {"z.py": "zzz@v1"}),
        snapshot("u-p1", {"a.py": "abc@v1", "new.py": None}),
    ])
    assert load_snapshot(episode) == {"a.py": "abc@v1", "new.py": None}


def test_no_matching_snapshot_loads_as_none(write_session):
    episode = only_episode(write_session, [human("x", "p1"), snapshot("u-other", {"a.py": "x@v1"})])
    assert load_snapshot(episode) is None


def test_a_replayable_episode_has_no_blockers(write_session, tmp_path):
    episode = only_episode(write_session, [human("x", "p1", cwd=str(tmp_path)), tool_use("Edit", file_path="a")])
    assert replay_blockers(episode, {"a": "x@v1"}) == []


def test_every_blocker_is_named(write_session, tmp_path):
    episode = only_episode(write_session, [
        human("x", "p1", cwd=str(tmp_path / "gone")),
        tool_use("mcp__slack__send_message", text="hi"),
        tool_use("AskUserQuestion", questions=[]),
    ])
    blockers = replay_blockers(episode, None)
    assert any("no longer exists" in b for b in blockers)
    assert any("snapshot" in b for b in blockers)
    assert not any("MCP" in b for b in blockers)  # MCP calls are replayed from recordings, never sent
    assert any("asked the human" in b for b in blockers)


def test_project_hooks_lists_every_hook_command_the_project_defines(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
        {"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "./notify.sh"}]}]}}))
    (tmp_path / ".claude" / "settings.local.json").write_text("{not json")
    assert project_hooks(tmp_path) == ["Stop: ./notify.sh", ".claude/settings.local.json is not valid JSON"]
    assert project_hooks(tmp_path / "elsewhere") == []


def test_the_session_root_is_the_first_working_directory_in_the_log(write_session):
    path = write_session([snapshot("u-p0", {}), human("a", "p0", cwd="/root"), human("b", "p1", cwd="/root/sub")])
    assert session_root(path) == Path("/root")


def test_a_git_worktree_or_submodule_working_directory_is_not_replayable(write_session, tmp_path):
    import subprocess
    main_repo = tmp_path / "main"
    subprocess.run(["git", "init", "-q", str(main_repo)], check=True)
    subprocess.run(["git", "-C", str(main_repo), "commit", "-q", "--allow-empty", "-m", "init"], check=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"})
    worktree = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main_repo), "worktree", "add", "-q", str(worktree)], check=True)
    episode = only_episode(write_session, [human("x", "p1", cwd=str(worktree))])
    assert any("worktree or submodule" in b for b in replay_blockers(episode, {}))


def test_the_home_directory_is_not_replayable(write_session, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    episode = only_episode(write_session, [human("x", "p1", cwd=str(home))])
    assert any("home directory" in b for b in replay_blockers(episode, {}))


def test_a_directory_containing_culprit_itself_is_not_replayable(write_session):
    import culprit
    culprit_repo = Path(culprit.__file__).resolve().parents[2]
    episode = only_episode(write_session, [human("x", "p1", cwd=str(culprit_repo.parent))])
    assert any("contains culprit" in b for b in replay_blockers(episode, {}))


def test_a_directory_whose_parent_is_not_writable_is_not_replayable(write_session, tmp_path):
    parent = tmp_path / "read-only"
    (parent / "proj").mkdir(parents=True)
    parent.chmod(0o555)
    try:
        episode = only_episode(write_session, [human("x", "p1", cwd=str(parent / "proj"))])
        assert any("not writable" in b for b in replay_blockers(episode, {}))
    finally:
        parent.chmod(0o755)


IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}


def test_a_prompt_whose_image_was_kept_in_the_log_is_replayable(write_session, tmp_path):
    episode = only_episode(write_session, [human([IMAGE, {"type": "text", "text": "fix this"}], "p1", cwd=str(tmp_path))])
    assert not any("image" in b for b in replay_blockers(episode, {}))


def test_a_prompt_whose_image_data_is_missing_is_not_replayable(write_session, tmp_path):
    episode = only_episode(write_session, [human([{"type": "image", "source": {}}, {"type": "text", "text": "fix"}],
                                                 "p1", cwd=str(tmp_path))])
    assert any("image" in b for b in replay_blockers(episode, {}))


def test_a_question_whose_answer_was_recorded_does_not_block_a_replay(write_session, tmp_path):
    answered = {"type": "user", "promptId": "p1", "toolUseResult": {"questions": [], "answers": {"Which db?": "Postgres"}},
                "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}
    episode = only_episode(write_session, [human("x", "p1", cwd=str(tmp_path)), tool_use("AskUserQuestion", questions=[]),
                                           answered])
    assert not any("asked the human" in b for b in replay_blockers(episode, {}))
