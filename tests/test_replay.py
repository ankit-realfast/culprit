import json
import subprocess
from pathlib import Path

import pytest

from culprit.episodes import episodes_from_session
from culprit.replay import (ReplaySettings, SetupScriptFailed, baseline_settings, project_folder_name,
                            replay_command, replay_permission_mode, replay_workdir, run_replay)
from fakes import FakeRun
from records import assistant, human

RESULT = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                     "total_cost_usd": 0.42, "num_turns": 7})
STREAM = "\n".join([json.dumps({"type": "system", "subtype": "init"}), json.dumps({"type": "assistant"}), RESULT])


def settings(**overrides):
    base = dict(prompt="fix the bug", model="claude-opus-5", permission_mode="auto")
    base.update(overrides)
    return ReplaySettings(**base)


def pristine_tree(tmp_path):
    pristine = tmp_path / "pristine"
    pristine.mkdir()
    (pristine / "a.py").write_text("old\n")
    return pristine


def test_permission_mode_keeps_auto_and_accept_edits_and_maps_everything_else_to_accept_edits():
    assert replay_permission_mode("auto") == "auto"
    assert replay_permission_mode("acceptEdits") == "acceptEdits"
    for other in ["default", "manual", "plan", "dontAsk", "bypassPermissions", None]:
        assert replay_permission_mode(other) == "acceptEdits"


def test_baseline_settings_copy_the_episode(write_session):
    (episode,) = episodes_from_session(write_session([human("fix it", "p1", permissionMode="default"),
                                                      assistant(model="claude-opus-4-8")]))
    s = baseline_settings(episode, max_budget_usd=1.5)
    assert (s.prompt, s.model, s.permission_mode, s.max_budget_usd) == ("fix it", "claude-opus-4-8", "acceptEdits", 1.5)
    assert (s.append_system_prompt, s.tools, s.disallowed_tools, s.context_preamble, s.setup_script) == (None, None, (), None, None)


def test_the_command_is_locked_down_and_carries_every_setting():
    command = replay_command(settings(append_system_prompt="verify first", tools="default",
                                      disallowed_tools=("WebFetch",), context_preamble="FILES"))
    joined = " ".join(command)
    assert command[:3] == ["claude", "-p", "FILES\n\nfix the bug"]
    for required in ["--output-format stream-json", "--verbose", "--strict-mcp-config",
                     '--mcp-config {"mcpServers":{}}', "--permission-prompts none", "--permission-mode auto",
                     "--no-session-persistence", "--max-budget-usd 2.00", "--model claude-opus-5",
                     "--tools default", "--append-system-prompt verify first"]:
        assert required in joined
    denied = command[command.index("--disallowedTools") + 1:]
    assert denied[:3] == ["Bash(git push *)", "Bash(gh *)", "WebFetch"]


def test_replay_workdir_sits_beside_the_original(write_session, tmp_path):
    (episode,) = episodes_from_session(write_session([human("x", "p1", cwd=str(tmp_path / "proj"))]))
    workdir = replay_workdir(episode)
    assert workdir.parent == tmp_path
    assert workdir.name.startswith(".culprit-replay-")
    assert replay_workdir(episode) != workdir


def test_project_folder_name_matches_claude_codes_encoding():
    assert project_folder_name(Path("/Users/akp/Downloads/New-Website")) == "-Users-akp-Downloads-New-Website"
    assert project_folder_name(Path("/private/var/T/tmp.hIy")) == "-private-var-T-tmp-hIy"


def test_a_run_records_everything_runs_checks_and_cleans_up(tmp_path):
    pristine = pristine_tree(tmp_path)
    projects = tmp_path / "projects"
    workdir = tmp_path / ".culprit-replay-abc"

    def claude_edits_and_leaves_memory(cwd):
        (cwd / "a.py").write_text("new\n")
        (projects / project_folder_name(cwd.resolve()) / "memory").mkdir(parents=True)

    run = FakeRun(claude_stdout=STREAM, check_codes={"pytest -q": 1}, diff="--- a.py\n+++ a.py\n",
                  on_claude=claude_edits_and_leaves_memory)
    result = run_replay(settings(), pristine, tmp_path / "run0", workdir, ("pytest -q", "true"), run=run,
                        projects_dir=projects)
    assert (result.is_error, result.result_text, result.cost_usd, result.num_turns) == (False, "done", 0.42, 7)
    assert [(c.command, c.exit_code) for c in result.checks] == [("pytest -q", 1), ("true", 0)]
    assert (tmp_path / "run0" / "transcript.jsonl").read_text() == STREAM
    assert (tmp_path / "run0" / "diff.patch").read_text().startswith("--- a.py")
    assert json.loads((tmp_path / "run0" / "result.json").read_text())["cost_usd"] == 0.42
    assert not workdir.exists()
    assert list(projects.iterdir()) == []
    assert (pristine / "a.py").read_text() == "old\n"
    claude_call = next(kwargs for command, kwargs in run.calls if command[0] == "claude")
    assert claude_call["cwd"] == workdir


def test_a_project_folder_with_a_session_log_is_kept(tmp_path):
    projects = tmp_path / "projects"
    workdir = tmp_path / ".culprit-replay-keep"

    def claude_leaves_a_log(cwd):
        folder = projects / project_folder_name(cwd.resolve())
        (folder / "memory").mkdir(parents=True)
        (folder / "real.jsonl").write_text("{}\n")

    run_replay(settings(), pristine_tree(tmp_path), tmp_path / "run0", workdir, (),
               run=FakeRun(claude_stdout=RESULT, on_claude=claude_leaves_a_log), projects_dir=projects)
    assert (projects / project_folder_name(workdir.resolve()) / "real.jsonl").exists()


def test_output_without_a_result_line_is_an_error(tmp_path):
    result = run_replay(settings(), pristine_tree(tmp_path), tmp_path / "run0", tmp_path / ".culprit-replay-x", (),
                        run=FakeRun(claude_stdout=json.dumps({"type": "assistant"})), projects_dir=tmp_path / "p")
    assert result.is_error is True
    assert "no result line" in result.result_text


def test_a_timed_out_replay_is_an_error_and_cleans_up(tmp_path):
    workdir = tmp_path / ".culprit-replay-slow"
    run = FakeRun(claude_raises=subprocess.TimeoutExpired(cmd="claude", timeout=1800))
    result = run_replay(settings(), pristine_tree(tmp_path), tmp_path / "run0", workdir, (), run=run,
                        projects_dir=tmp_path / "p")
    assert result.is_error is True
    assert "timed out" in result.result_text
    assert not workdir.exists()


def test_a_failing_setup_script_raises_and_cleans_up(tmp_path):
    workdir = tmp_path / ".culprit-replay-env"
    script = tmp_path / "fix.sh"
    script.write_text("exit 3\n")
    with pytest.raises(SetupScriptFailed, match="setup failed"):
        run_replay(settings(setup_script=script), pristine_tree(tmp_path), tmp_path / "run0", workdir, (),
                   run=FakeRun(setup_code=3), projects_dir=tmp_path / "p")
    assert not workdir.exists()


def test_a_relative_setup_script_runs_inside_the_clone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("runs").mkdir()
    Path("runs/env-fix.sh").write_text("touch fixed-by-setup.txt\n")
    seen = {}

    def run(command, **kwargs):
        if command[0] == "bash" and command[1] != "-c":
            done = subprocess.run(command, **kwargs)  # the real bash, run from the clone
            seen["fixed"] = (kwargs["cwd"] / "fixed-by-setup.txt").exists()
            return done
        return FakeRun(claude_stdout=RESULT)(command, **kwargs)

    run_replay(settings(setup_script=Path("runs/env-fix.sh")), pristine_tree(tmp_path), tmp_path / "run0",
               tmp_path / ".culprit-replay-rel", (), run=run, projects_dir=tmp_path / "p")
    assert seen["fixed"] is True


def test_git_in_a_replay_cannot_reach_a_repository_above_the_clone(tmp_path):
    parent_repo = tmp_path / "real-repo"
    parent_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(parent_repo)], check=True)
    seen = {}

    def run(command, **kwargs):
        if command[0] == "claude":
            probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=kwargs["cwd"],
                                   env=kwargs.get("env"), capture_output=True, text=True)
            seen["found_repo"] = probe.returncode == 0
            return FakeRun(claude_stdout=RESULT)(command, **kwargs)
        return FakeRun()(command, **kwargs)

    run_replay(settings(), pristine_tree(tmp_path), tmp_path / "run0", parent_repo / ".culprit-replay-git", (),
               run=run, projects_dir=tmp_path / "p")
    assert seen["found_repo"] is False
