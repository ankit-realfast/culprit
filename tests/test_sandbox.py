import subprocess

from culprit.episodes import episodes_from_session
from culprit.sandbox import check_restore, clone_tree, restore, strip_git_remotes
from records import edit_result, human


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("edited later\n")
    (repo / "created_later.py").write_text("did not exist at the episode start\n")
    (repo / "untouched.txt").write_text("never tracked\n")
    return repo


def make_history(tmp_path, session_id, backups):
    folder = tmp_path / "file-history" / session_id
    folder.mkdir(parents=True)
    for name, content in backups.items():
        (folder / name).write_text(content)
    return tmp_path / "file-history"


def episode_in(write_session, repo, extra=()):
    (episode,) = episodes_from_session(write_session([human("x", "p1", cwd=str(repo)), *extra]))
    return episode


def test_clone_copies_without_touching_the_source(tmp_path):
    repo = make_repo(tmp_path)
    clone_tree(repo, tmp_path / "clone")
    (tmp_path / "clone" / "untouched.txt").write_text("changed in the clone")
    assert (repo / "untouched.txt").read_text() == "never tracked\n"


def test_restore_overlays_backups_and_deletes_files_that_did_not_exist(write_session, tmp_path):
    repo = make_repo(tmp_path)
    history = make_history(tmp_path, "s1", {"aaa@v1": "original content\n"})
    episode = episode_in(write_session, repo)
    report = restore(episode, {"src/a.py": "aaa@v1", str(repo / "created_later.py"): None},
                     tmp_path / "restored", file_history_root=history)
    restored = tmp_path / "restored"
    assert (restored / "src" / "a.py").read_text() == "original content\n"
    assert not (restored / "created_later.py").exists()
    assert (restored / "untouched.txt").read_text() == "never tracked\n"
    assert (report.restored, report.deleted) == (1, 1)
    assert (repo / "src" / "a.py").read_text() == "edited later\n"  # the original is untouched


def test_a_missing_backup_is_reported(write_session, tmp_path):
    repo = make_repo(tmp_path)
    history = make_history(tmp_path, "s1", {})
    report = restore(episode_in(write_session, repo), {"src/a.py": "gone@v1"}, tmp_path / "restored",
                     file_history_root=history)
    assert report.missing_backups == ("src/a.py",)
    assert report.restored == 0


def test_paths_outside_the_working_directory_are_never_written(write_session, tmp_path):
    repo = make_repo(tmp_path)
    history = make_history(tmp_path, "s1", {"evil@v1": "pwned"})
    victim = tmp_path / "victim.txt"
    victim.write_text("safe")
    report = restore(episode_in(write_session, repo),
                     {"../victim.txt": "evil@v1", str(victim): "evil@v1"},
                     tmp_path / "restored", file_history_root=history)
    assert victim.read_text() == "safe"
    assert set(report.outside_cwd) == {"../victim.txt", str(victim)}


def test_git_remotes_are_removed_from_the_restored_copy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@example.com:x/y.git"], check=True)
    clone_tree(repo, tmp_path / "clone")
    strip_git_remotes(tmp_path / "clone")
    remotes = subprocess.run(["git", "-C", str(tmp_path / "clone"), "remote"], capture_output=True, text=True)
    assert remotes.stdout.strip() == ""
    original = subprocess.run(["git", "-C", str(repo), "remote"], capture_output=True, text=True)
    assert original.stdout.strip() == "origin"


def test_check_restore_compares_each_file_with_its_first_edit(write_session, tmp_path):
    repo = make_repo(tmp_path)
    episode = episode_in(write_session, repo, extra=[
        edit_result("p1", str(repo / "src" / "a.py"), "original content\n"),
        edit_result("p1", str(repo / "src" / "a.py"), "a later state\n"),
        edit_result("p1", str(repo / "untouched.txt"), "something else\n"),
    ])
    workdir = tmp_path / "restored"
    clone_tree(repo, workdir)
    (workdir / "src" / "a.py").write_text("original content\n")
    checked, mismatches = check_restore(episode, workdir)
    assert checked == 2
    assert [m.path for m in mismatches] == [str(repo / "untouched.txt")]
