import json
import subprocess
import sys

from culprit.answer_host import decide
from culprit.eligibility import recorded_answers
from culprit.episodes import episodes_from_session
from records import human

ANSWERS = {"How should I kick off #251?": "u decide whatever is best", "Which database?": "Postgres"}
QUESTION = {"question": "How should I start on #251?", "header": "Start", "multiSelect": False,
            "options": [{"label": "A", "description": "a"}, {"label": "B", "description": "b"}]}


def test_a_question_close_to_one_you_answered_gets_your_recorded_answer():
    decision = decide({"tool_name": "AskUserQuestion", "input": {"questions": [QUESTION]}}, ANSWERS)
    assert decision == {"behavior": "allow", "updatedInput": {
        "questions": [QUESTION], "answers": {"How should I start on #251?": "u decide whatever is best"}}}


def test_a_question_you_were_never_asked_is_refused():
    other = dict(QUESTION, question="What colour should the logo be?")
    decision = decide({"tool_name": "AskUserQuestion", "input": {"questions": [other]}}, ANSWERS)
    assert decision["behavior"] == "deny" and "not asked" in decision["message"]


def test_every_other_permission_request_is_refused_as_before():
    decision = decide({"tool_name": "Bash", "input": {"command": "rm -rf build"}}, ANSWERS)
    assert decision["behavior"] == "deny" and "no human" in decision["message"]


def test_recorded_answers_come_from_the_episodes_own_question_results(write_session):
    asked = {"type": "user", "promptId": "p1", "toolUseResult": {"questions": [QUESTION], "answers": {"Which database?": "Postgres"}},
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}
    (episode,) = episodes_from_session(write_session([human("set up the app", "p1"), asked]))
    assert recorded_answers(episode) == {"Which database?": "Postgres"}


def test_the_answer_host_speaks_json_rpc_over_stdio(tmp_path):
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(ANSWERS))
    call = {"tool_name": "AskUserQuestion", "input": {"questions": [QUESTION]}}
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "answer", "arguments": call}}]
    done = subprocess.run([sys.executable, "-m", "culprit.answer_host", str(path)],
                          input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True, text=True, timeout=30)
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["answer"]
    assert json.loads(replies[2]["result"]["content"][0]["text"])["behavior"] == "allow"
