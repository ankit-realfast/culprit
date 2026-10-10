import json
import subprocess
from pathlib import Path

import pytest

from culprit.episodes import episodes_from_session
from culprit.replay import (ReplaySettings, SetupScriptFailed, baseline_settings, project_folder_name,
                            replay_command, replay_prompt, replay_permission_mode, replay_workdir, run_replay)
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
    assert command[:2] == ["claude", "-p"]
    assert replay_prompt(settings(context_preamble="FILES")) == "FILES\n\nfix the bug"
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


def test_paths_into_the_original_project_point_at_the_clone_and_edits_there_are_denied(tmp_path):
    original = tmp_path / "proj"
    workdir = tmp_path / ".culprit-replay-x"
    command = replay_command(settings(prompt=f"fix {original}/a.py", context_preamble=f"=== {original}/b.py ==="),
                             original_cwd=original, workdir=workdir)
    assert replay_prompt(settings(prompt=f"fix {original}/a.py", context_preamble=f"=== {original}/b.py ==="),
                         original_cwd=original, workdir=workdir) == f"=== {workdir}/b.py ===\n\nfix {workdir}/a.py"
    denied = command[command.index("--disallowedTools") + 1:]
    for tool in ("Edit", "Write", "NotebookEdit"):
        assert f"{tool}(/{original}/**)" in denied


def test_a_replay_that_changes_the_original_project_is_stopped_loudly(tmp_path):
    from culprit.replay import OriginalModified
    original = tmp_path / "proj"
    original.mkdir()
    (original / "a.py").write_text("untouched\n")

    def claude_escapes(cwd):
        (original / "a.py").write_text("written by the replay\n")

    with pytest.raises(OriginalModified, match="a.py"):
        run_replay(settings(), pristine_tree(tmp_path), tmp_path / "run0", tmp_path / ".culprit-replay-esc", (),
                   run=FakeRun(claude_stdout=RESULT, on_claude=claude_escapes), projects_dir=tmp_path / "p",
                   original_cwd=original)


@pytest.mark.parametrize("result_line, void", [
    ({"type": "result", "subtype": "success", "is_error": False, "result": "ok"}, None),
    ({"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "result": ""}, "error_max_budget_usd"),
    ({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 529, "result": ""}, "API error 529"),
])
def test_runs_that_fail_for_reasons_outside_the_agent_are_void(tmp_path, result_line, void):
    result = run_replay(settings(), pristine_tree(tmp_path), tmp_path / "run0", tmp_path / ".culprit-replay-v", (),
                        run=FakeRun(claude_stdout=json.dumps(result_line)), projects_dir=tmp_path / "p")
    assert (result.void_reason is None) == (void is None)
    if void:
        assert void in result.void_reason


def test_a_timed_out_or_resultless_run_is_void(tmp_path):
    timed_out = run_replay(settings(), pristine_tree(tmp_path), tmp_path / "run0", tmp_path / ".culprit-replay-t", (),
                           run=FakeRun(claude_raises=subprocess.TimeoutExpired(cmd="claude", timeout=1)),
                           projects_dir=tmp_path / "p")
    assert "timed out" in timed_out.void_reason


def test_a_run_whose_cost_is_unknown_is_charged_its_cap(tmp_path):
    result = run_replay(settings(max_budget_usd=1.5), pristine_tree(tmp_path), tmp_path / "run0",
                        tmp_path / ".culprit-replay-c", (),
                        run=FakeRun(claude_raises=subprocess.TimeoutExpired(cmd="claude", timeout=1)),
                        projects_dir=tmp_path / "p")
    assert result.cost_usd == 1.5


def test_the_prompt_goes_on_stdin_so_a_leading_dash_is_never_an_option(tmp_path):
    command = replay_command(settings(prompt="-v fix the flag parser"))
    assert command[:2] == ["claude", "-p"] and "-v fix the flag parser" not in command
    run = FakeRun(claude_stdout=RESULT)
    run_replay(settings(prompt="-v fix the flag parser"), pristine_tree(tmp_path), tmp_path / "run0",
               tmp_path / ".culprit-replay-dash", (), run=run, projects_dir=tmp_path / "p")
    claude_kwargs = next(kwargs for command, kwargs in run.calls if command[0] == "claude")
    assert claude_kwargs["input"] == "-v fix the flag parser"


def test_a_timeout_kills_every_process_the_replay_started():
    import time
    from culprit.replay import run_in_own_process_group
    marker = "37.4242"
    with pytest.raises(subprocess.TimeoutExpired):
        run_in_own_process_group(["bash", "-c", f"sleep {marker} & sleep {marker}"], capture_output=True,
                                 text=True, timeout=1)
    time.sleep(0.3)
    left = subprocess.run(["pgrep", "-f", f"sleep {marker}"], capture_output=True, text=True)
    assert left.stdout.strip() == ""


def test_the_stored_diff_skips_dependency_folders_and_is_bounded(tmp_path):
    from culprit.replay import MAX_DIFF_BYTES
    run = FakeRun(claude_stdout=RESULT, diff="+x\n" * (MAX_DIFF_BYTES // 3 + 1000))
    run_replay(settings(), pristine_tree(tmp_path), tmp_path / "run0", tmp_path / ".culprit-replay-diff", (),
               run=run, projects_dir=tmp_path / "p")
    diff_command = next(command for command, _ in run.calls if command[0] == "diff")
    assert "--exclude=node_modules" in diff_command and "--exclude=.venv" in diff_command
    stored = (tmp_path / "run0" / "diff.patch").read_text()
    assert len(stored.encode()) <= MAX_DIFF_BYTES + 200 and "[diff cut" in stored


def test_every_replay_runs_inside_the_bash_sandbox():
    command = replay_command(settings())
    applied = json.loads(command[command.index("--settings") + 1])
    assert applied["sandbox"] == {"enabled": True, "allowUnsandboxedCommands": False, "failIfUnavailable": True}


def test_the_replay_reads_a_copy_of_the_original_projects_memory(tmp_path):
    memory = tmp_path / "project-logs" / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("- tests live in spec/\n")
    run = FakeRun(claude_stdout=RESULT, on_claude=lambda cwd: None)
    run_replay(settings(), pristine_tree(tmp_path), tmp_path / "run0", tmp_path / ".culprit-replay-mem", (), run=run,
               projects_dir=tmp_path / "p", original_memory=memory)
    command = next(command for command, _ in run.calls if command[0] == "claude")
    applied = json.loads(command[command.index("--settings") + 1])
    copy = Path(applied["autoMemoryDirectory"])
    assert copy == (tmp_path / "run0" / "memory").resolve()
    assert (copy / "MEMORY.md").read_text() == "- tests live in spec/\n"
    assert (memory / "MEMORY.md").read_text() == "- tests live in spec/\n"


IMAGE_BLOCK = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}


def test_baseline_settings_carry_the_prompts_images(write_session):
    (episode,) = episodes_from_session(write_session([human([IMAGE_BLOCK, {"type": "text", "text": "fix this"}], "p1")]))
    assert baseline_settings(episode).prompt_images == (IMAGE_BLOCK,)


def test_a_prompt_with_images_is_sent_as_one_stream_json_message(tmp_path):
    run = FakeRun(claude_stdout=RESULT)
    run_replay(settings(prompt_images=(IMAGE_BLOCK,)), pristine_tree(tmp_path), tmp_path / "run0",
               tmp_path / ".culprit-replay-img", (), run=run, projects_dir=tmp_path / "p")
    command, kwargs = next((c, k) for c, k in run.calls if c[0] == "claude")
    assert command[command.index("--input-format") + 1] == "stream-json"
    message = json.loads(kwargs["input"])
    assert message["type"] == "user"
    assert message["message"]["content"] == [IMAGE_BLOCK, {"type": "text", "text": "fix the bug"}]


def test_settings_with_images_round_trip_through_json():
    from culprit.replay import settings_from_json, settings_to_json
    with_images = settings(prompt_images=(IMAGE_BLOCK,))
    assert settings_from_json(json.loads(json.dumps(settings_to_json(with_images)))) == with_images


NOTES = {"notes": [{"tool": "get_note", "arguments": {"id": "1"}, "text": "ZEBRA", "is_error": False}]}


def test_baseline_settings_carry_the_episodes_mcp_recordings(write_session):
    call = {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5", "content": [
        {"type": "tool_use", "id": "t1", "name": "mcp__notes__get_note", "input": {"id": "1"}}]}}
    result = {"type": "user", "promptId": "p1", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ZEBRA"}]}}
    (episode,) = episodes_from_session(write_session([human("check", "p1"), call, result]))
    assert baseline_settings(episode).mcp_recordings == NOTES


def test_recorded_mcp_servers_are_replaced_by_the_stand_in(tmp_path):
    import sys
    run = FakeRun(claude_stdout=RESULT)
    run_replay(settings(mcp_recordings=NOTES), pristine_tree(tmp_path), tmp_path / "run0",
               tmp_path / ".culprit-replay-mcp", (), run=run, projects_dir=tmp_path / "p")
    command = next(c for c, _ in run.calls if c[0] == "claude")
    config = json.loads(command[command.index("--mcp-config") + 1])
    stub = config["mcpServers"]["notes"]
    assert stub["command"] == sys.executable and stub["args"][:2] == ["-m", "culprit.mcp_stub"]
    assert json.loads(Path(stub["args"][2]).read_text()) == NOTES
    assert "--strict-mcp-config" in command
    assert "mcp__notes" in command[command.index("--allowedTools") + 1:]
