import json
import subprocess
import sys
from pathlib import Path

import pytest

from culprit.logs import episodes_from_session
from culprit.replay import ReplaySettings, baseline_settings, replay_command, run_replay, settings_from_json, settings_to_json
from culprit.stubs import (MAX_WEB_RESULT_CHARS, NETWORK_LOG, bash_hook, network_recordings_from_episode,
                           prepare_network_recordings, uses_network, web_hook)
from fakes import FakeRun
from records import human

RESULT = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                     "total_cost_usd": 0.42, "num_turns": 7})


def bash_call(tool_id, command):
    return {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5", "content": [
        {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command, "description": "d"}}]}}


def bash_result(prompt_id, tool_id, text, is_error=False):
    return {"type": "user", "promptId": prompt_id, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": text, "is_error": is_error}]}}


# --- which shell commands reach the network ---

@pytest.mark.parametrize("command", [
    "curl -s https://api.example.com/v1/items",
    "wget -q https://example.com/file.tgz",
    "gcloud run services list", "gsutil ls gs://bucket", "bq query 'select 1'",
    "psql -h db.internal -c 'select 1'", "mysql -h db -e 'show tables'",
    "aws s3 ls", "az account show", "kubectl get pods -n prod",
    "ssh deploy@host 'uptime'", "scp build.tgz host:/srv/", "sftp host",
    "rsync -az dist/ deploy@host:/srv/app", "rsync -e ssh -az dist/ /mnt/backup",
    "http GET api.example.com/items", "https POST api.example.com/items name=x",
    "redis-cli -h cache ping", "mongosh mongodb://db/app", "sqlcmd -S server -Q 'select 1'",
    "firebase deploy", "vercel ls", "supabase db pull", "heroku logs --app x", "stripe customers list",
    "nc -z host 443", "telnet host 25", "doctl apps list", "flyctl status", "netlify status",
    "wrangler deploy", "helm list", "terraform plan",
    # at command position anywhere in the line, behind wrappers, assignments and full paths
    "cd app && curl -s localhost:8080/health",
    "TOKEN=abc /usr/bin/curl -H \"auth: $TOKEN\" https://x",
    "sudo -E kubectl apply -f k8s.yaml",
    "timeout 30 psql -c 'select 1'",
    "echo hi | ssh host cat",
    "x=$(curl -s https://x) && echo $x",
    "bash -c 'aws s3 ls'",
    "env AWS_PROFILE=prod aws s3 ls",
])
def test_network_tools_are_recognised_wherever_they_run(command):
    assert uses_network(command)


@pytest.mark.parametrize("command", [
    "ls -la", "pytest -q", "grep -r curl src/", "echo 'use curl to fetch'", "cat ssh_config",
    "python -m http.server --help", "rsync -a src/ backup/",
    # gh and git push stay denied by the replay's deny rules, never answered from a recording
    "gh pr list", "git push origin main", "git status",
])
def test_local_commands_are_not_network_calls(command):
    assert not uses_network(command)


# --- recordings from the episode ---

def test_network_commands_are_recorded_with_their_output_and_exit_status(write_session):
    (episode,) = episodes_from_session(write_session([
        human("deploy", "p1"),
        bash_call("t1", "kubectl get pods"), bash_result("p1", "t1", "web-1 Running"),
        bash_call("t2", "pytest -q"), bash_result("p1", "t2", "3 passed"),
        bash_call("t3", "curl -sf https://x/health"), bash_result("p1", "t3", "Exit code 22\ncurl: (22) 503", is_error=True),
        bash_call("t4", "psql -c 'select 1'"), bash_result("p1", "t4", "permission denied by hook", is_error=True),
    ]))
    assert network_recordings_from_episode(episode)["shell"] == [
        {"command": "kubectl get pods", "output": "web-1 Running", "exit_code": 0},
        {"command": "curl -sf https://x/health", "output": "curl: (22) 503", "exit_code": 22},
        {"command": "psql -c 'select 1'", "output": "permission denied by hook", "exit_code": 1},
    ]


def test_baseline_settings_carry_the_network_recordings_and_survive_json(write_session):
    (episode,) = episodes_from_session(write_session([
        human("deploy", "p1"), bash_call("t1", "kubectl get pods"), bash_result("p1", "t1", "web-1 Running")]))
    settings = baseline_settings(episode)
    assert settings.network_recordings["shell"] == [{"command": "kubectl get pods", "output": "web-1 Running",
                                                     "exit_code": 0}]
    assert settings_from_json(json.loads(json.dumps(settings_to_json(settings)))) == settings


# --- the Bash hook ---

def prepared(tmp_path, shell, rewrite=lambda text: text):
    return prepare_network_recordings({"shell": shell}, tmp_path / "run0", rewrite)


def event(command):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": command, "description": "check", "timeout": 5000}}


def test_an_exactly_recorded_command_is_rewritten_to_print_its_recorded_output(tmp_path):
    path = prepared(tmp_path, [{"command": "kubectl  get pods", "output": "web-1 Running\n", "exit_code": 0},
                               {"command": "curl -sf https://x/health", "output": "curl: (22) 503", "exit_code": 22}])
    reply = bash_hook(event("curl  -sf\nhttps://x/health"), path)  # whitespace differences do not matter
    output = reply["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse" and output["permissionDecision"] == "allow"
    assert output["updatedInput"]["description"] == "check" and output["updatedInput"]["timeout"] == 5000
    replaced = subprocess.run(["bash", "-c", output["updatedInput"]["command"]], capture_output=True, text=True)
    assert (replaced.stdout, replaced.returncode) == ("curl: (22) 503", 22)


def test_the_recorded_command_is_matched_after_original_paths_point_at_the_clone(tmp_path):
    path = prepared(tmp_path, [{"command": "scp /repo/dist.tgz host:/srv", "output": "ok", "exit_code": 0}],
                    rewrite=lambda text: text.replace("/repo", "/.culprit-replay-x"))
    assert bash_hook(event("scp /.culprit-replay-x/dist.tgz host:/srv"), path) is not None
    assert bash_hook(event("scp /repo/dist.tgz host:/srv"), path) is None


def test_a_repeated_command_gets_each_recorded_answer_in_turn_then_the_last_again(tmp_path):
    path = prepared(tmp_path, [{"command": "kubectl get pods", "output": "Pending", "exit_code": 0},
                               {"command": "kubectl get pods", "output": "Running", "exit_code": 0}])
    outputs = []
    for _ in range(3):
        command = bash_hook(event("kubectl get pods"), path)["hookSpecificOutput"]["updatedInput"]["command"]
        outputs.append(subprocess.run(["bash", "-c", command], capture_output=True, text=True).stdout)
    assert outputs == ["Pending", "Running", "Running"]


def test_anything_unrecorded_is_left_to_the_sandbox_and_network_calls_are_logged(tmp_path):
    path = prepared(tmp_path, [{"command": "kubectl get pods", "output": "Running", "exit_code": 0}])
    assert bash_hook(event("kubectl get pods -n prod"), path) is None  # unrecorded: the sandbox refuses it
    assert bash_hook(event("pytest -q"), path) is None  # not a network call: not logged
    assert bash_hook(event("kubectl get pods"), path) is not None
    assert bash_hook({"tool_name": "Read", "tool_input": {"file_path": "x"}}, path) is None
    log = [json.loads(line) for line in (path.parent / NETWORK_LOG).read_text().splitlines()]
    assert [(entry["tool"], entry["call"], entry["served"]) for entry in log] == [
        ("Bash", "kubectl get pods -n prod", False), ("Bash", "kubectl get pods", True)]


def test_the_hook_process_reads_the_event_on_stdin_and_prints_its_json(tmp_path):
    path = prepared(tmp_path, [{"command": "aws s3 ls", "output": "bucket-a", "exit_code": 0}])
    hit = subprocess.run([sys.executable, "-m", "culprit.stubs", "bash-hook", str(path)],
                         input=json.dumps(event("aws s3 ls")), capture_output=True, text=True, timeout=30)
    assert hit.returncode == 0 and json.loads(hit.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    miss = subprocess.run([sys.executable, "-m", "culprit.stubs", "bash-hook", str(path)],
                          input=json.dumps(event("ls")), capture_output=True, text=True, timeout=30)
    assert (miss.returncode, miss.stdout) == (0, "")


def test_the_hook_stays_light_importing_nothing_of_culprit_beyond_logs():
    loaded = subprocess.run([sys.executable, "-c", "import sys, culprit.stubs; "
                             "print(sorted(m for m in sys.modules if m.startswith('culprit')))"],
                            capture_output=True, text=True, timeout=30)
    assert loaded.stdout.strip() == "['culprit', 'culprit.logs', 'culprit.stubs']"


# --- wired into the replay ---

def test_every_replay_adds_culprits_bash_hook_beside_the_users_own(tmp_path):
    shell = [{"command": "aws s3 ls", "output": "bucket-a", "exit_code": 0}]
    settings = ReplaySettings(prompt="fix", model=None, permission_mode="auto", network_recordings={"shell": shell})
    pristine = tmp_path / "pristine"
    pristine.mkdir()
    run = FakeRun(claude_stdout=RESULT)
    run_replay(settings, pristine, tmp_path / "run0", tmp_path / ".culprit-replay-net", (), run=run,
               projects_dir=tmp_path / "p")
    command = next(c for c, _ in run.calls if c[0] == "claude")
    applied = json.loads(command[command.index("--settings") + 1])
    (bash,) = [entry for entry in applied["hooks"]["PreToolUse"] if entry["matcher"] == "Bash"]
    (hook,) = bash["hooks"]
    assert hook["type"] == "command" and f"{sys.executable} -m culprit.stubs bash-hook " in hook["command"]
    recordings = (tmp_path / "run0" / "network-recordings.json").resolve()
    assert str(recordings) in hook["command"] and json.loads(recordings.read_text())["shell"][0]["command"] == "aws s3 ls"
    assert "allowManagedHooksOnly" not in applied and "disableAllHooks" not in applied  # the user's hooks still run


# --- WebFetch and WebSearch: never live ---

def web_call(tool_id, name, **tool_input):
    return {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5", "content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]}}


def web_event(name, **tool_input):
    return {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": tool_input}


WEB = [{"tool": "WebFetch", "input": {"url": "https://docs.x/api", "prompt": "list the endpoints"}, "text": "GET /items"},
       {"tool": "WebSearch", "input": {"query": "x api rate limit"}, "text": "100 requests per minute"}]


def test_web_calls_are_recorded_with_their_results(write_session):
    (episode,) = episodes_from_session(write_session([
        human("read the docs", "p1"),
        web_call("t1", "WebFetch", url="https://docs.x/api", prompt="list the endpoints"),
        bash_result("p1", "t1", [{"type": "text", "text": "GET /items"}]),
        web_call("t2", "WebSearch", query="x api rate limit", allowed_domains=["x.com"]),
        bash_result("p1", "t2", "100 requests per minute"),
    ]))
    assert network_recordings_from_episode(episode)["web"] == WEB


def test_an_exactly_recorded_web_call_is_denied_with_its_recorded_result(tmp_path):
    path = prepare_network_recordings({"web": WEB}, tmp_path / "run0", lambda text: text)
    fetched = web_hook(web_event("WebFetch", url="https://docs.x/api", prompt="list the endpoints"), path)
    output = fetched["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse" and output["permissionDecision"] == "deny"
    assert output["permissionDecisionReason"] == ("offline replay; the recorded result of this exact call was: "
                                                  "GET /items")
    searched = web_hook(web_event("WebSearch", query="x api rate limit", allowed_domains=["y.com"]), path)
    assert searched["hookSpecificOutput"]["permissionDecisionReason"].endswith("100 requests per minute")


def test_any_other_web_call_is_denied_as_not_recorded(tmp_path):
    path = prepare_network_recordings({"web": WEB}, tmp_path / "run0", lambda text: text)
    for call in (web_event("WebFetch", url="https://docs.x/api", prompt="summarise"),
                 web_event("WebSearch", query="x api pricing")):
        output = web_hook(call, path)["hookSpecificOutput"]
        assert (output["permissionDecision"], output["permissionDecisionReason"]) == ("deny", "offline replay: not recorded")
    log = [json.loads(line) for line in (path.parent / NETWORK_LOG).read_text().splitlines()]
    assert [(entry["tool"], entry["served"]) for entry in log] == [("WebFetch", False), ("WebSearch", False)]


def test_a_long_recorded_result_is_cut_to_a_bounded_size(tmp_path):
    long = [{"tool": "WebSearch", "input": {"query": "q"}, "text": "x" * (MAX_WEB_RESULT_CHARS * 2)}]
    path = prepare_network_recordings({"web": long}, tmp_path / "run0", lambda text: text)
    reason = web_hook(web_event("WebSearch", query="q"), path)["hookSpecificOutput"]["permissionDecisionReason"]
    assert len(reason) < MAX_WEB_RESULT_CHARS + 200 and "[recorded result cut" in reason


def test_the_web_hook_denies_even_when_it_cannot_read_its_recordings(tmp_path):
    done = subprocess.run([sys.executable, "-m", "culprit.stubs", "web-hook", str(tmp_path / "missing.json")],
                          input=json.dumps(web_event("WebFetch", url="https://x", prompt="p")),
                          capture_output=True, text=True, timeout=30)
    assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_every_replay_puts_culprits_web_hook_in_front_of_webfetch_and_websearch(tmp_path):
    pristine = tmp_path / "pristine"
    pristine.mkdir()
    run = FakeRun(claude_stdout=RESULT)
    run_replay(ReplaySettings(prompt="fix", model=None, permission_mode="auto"), pristine, tmp_path / "run0",
               tmp_path / ".culprit-replay-web", (), run=run, projects_dir=tmp_path / "p")
    command = next(c for c, _ in run.calls if c[0] == "claude")
    applied = json.loads(command[command.index("--settings") + 1])
    (web,) = [entry for entry in applied["hooks"]["PreToolUse"] if entry["matcher"] == "WebFetch|WebSearch"]
    assert " -m culprit.stubs web-hook " in web["hooks"][0]["command"]


def test_a_command_built_without_culprits_hooks_denies_the_web_tools_outright():
    command = replay_command(ReplaySettings(prompt="fix", model=None, permission_mode="auto"))
    denied = command[command.index("--disallowedTools") + 1:]
    assert "WebFetch" in denied and "WebSearch" in denied
