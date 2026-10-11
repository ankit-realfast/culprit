import json

from culprit.model import MAX_DIFF_CHARS, Criterion, Judgement, actions_from_transcript, judge_replay, judging_prompt
from culprit.replay import CheckResult, ReplayResult
from fakes import FakeAsk

CRITERION = Criterion("the export has a header row", ("pytest -q",), ("delete tests",), approved=True)


def replay_in(tmp_path, checks=(), is_error=False, diff="+header\n"):
    run_dir = tmp_path / "run0"
    run_dir.mkdir()
    (run_dir / "diff.patch").write_text(diff)
    return ReplayResult(run_dir, 0, is_error, "I added the header", 0.3, 4, tuple(checks))


def test_a_failing_check_fails_without_asking_a_model(tmp_path):
    ask = FakeAsk()
    judgement = judge_replay(CRITERION, replay_in(tmp_path, [CheckResult("pytest -q", 1, "1 failed")]), ask=ask)
    assert judgement == Judgement(False, "check failed: pytest -q (exit 1)", "check")
    assert ask.calls == []


def test_an_errored_replay_fails_without_asking_a_model(tmp_path):
    ask = FakeAsk()
    judgement = judge_replay(CRITERION, replay_in(tmp_path, is_error=True), ask=ask)
    assert (judgement.passed, judgement.by) == (False, "replay")
    assert ask.calls == []


def test_otherwise_the_model_decides_from_criterion_answer_and_diff(tmp_path):
    ask = FakeAsk({"passed": True, "reason": "header present, tests intact"})
    judgement = judge_replay(CRITERION, replay_in(tmp_path, [CheckResult("pytest -q", 0, "ok")]), ask=ask)
    assert judgement == Judgement(True, "header present, tests intact", "model")
    prompt = ask.calls[0][0]
    for expected in ["the export has a header row", "delete tests", "I added the header", "+header", "pytest -q"]:
        assert expected in prompt


def test_a_huge_diff_is_truncated_in_the_prompt(tmp_path):
    replay = replay_in(tmp_path, diff="x" * (MAX_DIFF_CHARS + 5000))
    prompt = judging_prompt(CRITERION, replay, (replay.run_dir / "diff.patch").read_text(), "")
    assert "[diff truncated" in prompt
    assert prompt.count("x") <= MAX_DIFF_CHARS + 100


def test_the_judge_sees_every_tool_call_and_the_effort_spent(tmp_path):
    replay = replay_in(tmp_path, [CheckResult("pytest -q", 0, "ok")])
    (replay.run_dir / "transcript.jsonl").write_text("\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "rm -rf tests"}}]}}),
        json.dumps({"type": "result", "subtype": "success"})]))
    assert actions_from_transcript((replay.run_dir / "transcript.jsonl").read_text()) == \
        '- Bash: {"command": "rm -rf tests"}'
    ask = FakeAsk({"passed": False, "reason": "deleted the tests"})
    judge_replay(CRITERION, replay, ask=ask)
    prompt = ask.calls[0][0]
    assert "rm -rf tests" in prompt and "4 turns" in prompt


def test_a_void_run_is_not_judged(tmp_path):
    from dataclasses import replace
    ask = FakeAsk()
    judgement = judge_replay(CRITERION, replace(replay_in(tmp_path), void_reason="API error 529"), ask=ask)
    assert (judgement.passed, judgement.by) == (False, "void")
    assert ask.calls == []


def test_a_run_stopped_at_a_limit_is_judged_on_what_it_produced(tmp_path):
    from dataclasses import replace
    ask = FakeAsk({"passed": False, "reason": "unfinished"})
    replay = replace(replay_in(tmp_path, [CheckResult("pytest -q", 0, "ok")], is_error=True), limit="budget")
    judgement = judge_replay(CRITERION, replay, ask=ask)
    assert judgement == Judgement(False, "unfinished", "model")
    assert "spend limit" in ask.calls[0][0]


def test_a_run_stopped_at_a_limit_still_fails_a_failing_check_without_a_model(tmp_path):
    from dataclasses import replace
    ask = FakeAsk()
    replay = replace(replay_in(tmp_path, [CheckResult("pytest -q", 1, "1 failed")], is_error=True), limit="time")
    assert judge_replay(CRITERION, replay, ask=ask).by == "check"
    assert ask.calls == []


def test_a_model_judgement_keeps_the_model_that_judged(tmp_path):
    from culprit.model import MODEL_RAN_KEY
    ask = FakeAsk({"passed": False, "reason": "no header", MODEL_RAN_KEY: "claude-sonnet-5-5"})
    judgement = judge_replay(CRITERION, replay_in(tmp_path), ask=ask)
    assert judgement == Judgement(False, "no header", "model", model="claude-sonnet-5-5")


def test_a_drafted_criterion_keeps_the_model_that_drafted_it(tmp_path):
    from culprit.logs import Episode
    from culprit.model import MODEL_RAN_KEY, draft_criterion, load_criterion, save_criterion
    from records import human
    from culprit.logs import episodes_from_session
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps(human("fix", "p1", session_id="s1")) + "\n")
    (episode,) = episodes_from_session(path)
    ask = FakeAsk({"success_means": "works", "checks": [], "must_not": [], MODEL_RAN_KEY: "claude-sonnet-5-5"})
    criterion = draft_criterion(episode, [], ask=ask)
    assert criterion.model == "claude-sonnet-5-5"
    save_criterion(tmp_path / "runs", "s1:p1", criterion)
    assert load_criterion(tmp_path / "runs", "s1:p1").model == "claude-sonnet-5-5"
