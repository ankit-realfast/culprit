import json

import pytest

from culprit.model import ClaudeCallFailed, ask_claude, structured_call_command
from culprit.model import resolve_alias  # the real one: conftest stands a fake in for culprit.model.resolve_alias
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


def test_calls_ask_for_the_sonnet_alias_unless_culprit_drafting_model_says_otherwise(tmp_path, monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return completed(json.dumps({"is_error": False, "subtype": "success", "structured_output": {"ok": True}}))

    ask_claude("q", SCHEMA, run=run, cwd=tmp_path)
    monkeypatch.setenv("CULPRIT_DRAFTING_MODEL", "claude-sonnet-5-5")
    ask_claude("q", SCHEMA, run=run, cwd=tmp_path)
    assert [c[c.index("--model") + 1] for c in commands] == ["sonnet", "claude-sonnet-5-5"]


INIT = json.dumps({"type": "system", "subtype": "init", "model": "claude-fable-5-1"})


def stream_result(cost):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": cost,
                       "num_turns": 0})


def test_an_alias_is_resolved_for_free_by_a_local_command_that_still_names_the_model(tmp_path):
    from culprit.model import Resolution
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return completed(INIT + "\n" + stream_result(0) + "\n")

    assert resolve_alias("fable", run=run, cwd=tmp_path) == Resolution("claude-fable-5-1", 0.0)
    [(command, kwargs)] = calls
    joined = " ".join(command)
    for required in ["-p", "--model fable", "--output-format stream-json", "--verbose", "--no-session-persistence",
                     "--max-budget-usd 0.05", "--strict-mcp-config", "--permission-prompts none"]:
        assert required in joined
    assert command[command.index("--tools") + 1] == ""
    assert kwargs["input"] == "/cost" and kwargs["cwd"] == tmp_path


def test_when_the_free_way_names_no_model_one_capped_trivial_call_does(tmp_path):
    from culprit.model import RESOLVE_CAP_USD, Resolution
    inputs = []

    def run(command, **kwargs):
        inputs.append(kwargs["input"])
        return completed(stream_result(0)) if len(inputs) == 1 else completed(INIT + "\n" + stream_result(0.003))

    assert resolve_alias("fable", run=run, cwd=tmp_path) == Resolution("claude-fable-5-1", 0.003)
    assert inputs[0] == "/cost" and inputs[1] != "/cost"

    def broken(command, **kwargs):
        return completed("", stderr="boom", returncode=1)

    # a paid attempt that reports no cost is charged at its cap
    assert resolve_alias("fable", run=broken, cwd=tmp_path) == Resolution(None, RESOLVE_CAP_USD)


def test_a_resolved_alias_is_cached_in_the_store_for_a_day(tmp_path, monkeypatch):
    import culprit.model as model
    asked = []

    def resolve(alias):
        asked.append(alias)
        return model.Resolution(f"claude-{alias}-{len(asked)}", 0.05)

    monkeypatch.setattr(model, "resolve_alias", resolve)
    runs = tmp_path / "runs"
    assert model.resolved_model(runs, "fable", now=1000.0) == model.Resolution("claude-fable-1", 0.05)
    assert model.resolved_model(runs, "fable", now=1000.0 + 3600) == model.Resolution("claude-fable-1", 0.0)
    assert model.cached_model(runs, "fable", now=1000.0 + 3600) == "claude-fable-1"
    assert json.loads((runs / "models.json").read_text())["fable"]["model"] == "claude-fable-1"
    assert model.resolved_model(runs, "fable", now=1000.0 + 86400 + 1).model == "claude-fable-2"
    assert model.cached_model(runs, "opus") is None
    assert asked == ["fable", "fable"]


def test_an_alias_that_cannot_be_resolved_is_not_cached(tmp_path, monkeypatch):
    import culprit.model as model
    monkeypatch.setattr(model, "resolve_alias", lambda alias: model.Resolution(None, 0.05))
    assert model.resolved_model(tmp_path / "runs", "fable") == model.Resolution(None, 0.05)
    assert model.cached_model(tmp_path / "runs", "fable") is None
