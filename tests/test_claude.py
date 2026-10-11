import json

import pytest

from culprit.model import ClaudeCallFailed, ask_claude, structured_call_command
from fakes import completed

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def test_the_command_is_locked_down():
    command = structured_call_command("judge this", SCHEMA, "claude-sonnet-5-5", 0.5)
    assert command[:3] == ["claude", "-p", "judge this"]
    joined = " ".join(command)
    for required in ["--output-format json", "--strict-mcp-config", '--mcp-config {"mcpServers":{}}',
                     "--permission-prompts none", "--no-session-persistence", "--max-budget-usd 0.50",
                     "--model claude-sonnet-5-5"]:
        assert required in joined
    assert command[command.index("--tools") + 1] == ""
    assert json.loads(command[command.index("--json-schema") + 1]) == SCHEMA


def test_structured_output_is_returned_from_a_call_run_in_its_own_folder(tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs)
        return completed(json.dumps({"is_error": False, "subtype": "success", "structured_output": {"ok": True}}))

    assert ask_claude("q", SCHEMA, run=run, cwd=tmp_path / "calls") == {"ok": True}
    assert calls[0]["capture_output"] is True
    assert calls[0]["cwd"] == tmp_path / "calls"
    assert (tmp_path / "calls").is_dir()


def test_a_failed_call_raises_with_the_reason(tmp_path):
    def run(command, **kwargs):
        return completed(json.dumps({"is_error": True, "subtype": "error_max_budget_usd", "result": "over budget"}))

    with pytest.raises(ClaudeCallFailed, match="over budget"):
        ask_claude("q", SCHEMA, run=run, cwd=tmp_path)


def test_non_json_output_raises_with_stderr(tmp_path):
    def run(command, **kwargs):
        return completed("", stderr="Error: --json-schema is not a valid JSON Schema", returncode=1)

    with pytest.raises(ClaudeCallFailed, match="not a valid JSON Schema"):
        ask_claude("q", SCHEMA, run=run, cwd=tmp_path)


def test_a_call_that_times_out_raises_the_named_error(tmp_path):
    import subprocess

    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=600)

    with pytest.raises(ClaudeCallFailed, match="timed out"):
        ask_claude("q", SCHEMA, run=run, cwd=tmp_path)


def test_the_model_a_call_ran_as_comes_back_beside_the_answer(tmp_path):
    def run(command, **kwargs):
        return completed(json.dumps({"is_error": False, "subtype": "success", "structured_output": {"ok": True},
                                     "modelUsage": {"claude-haiku-5-5": {"outputTokens": 3},
                                                    "claude-sonnet-5-5": {"outputTokens": 120}}}))

    from culprit.model import MODEL_RAN_KEY
    assert ask_claude("q", SCHEMA, run=run, cwd=tmp_path) == {"ok": True, MODEL_RAN_KEY: "claude-sonnet-5-5"}
