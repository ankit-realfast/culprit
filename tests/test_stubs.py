import json
import subprocess
import sys

from culprit.logs import episodes_from_session, recorded_answers
from culprit.stubs import decide, handle, recordings_from_episode
from records import human

RECORDINGS = {"notes": [{"tool": "get_note", "arguments": {"id": "1"}, "text": "The note says ZEBRA", "is_error": False}]}


def mcp_call(prompt_id, tool_id, name, arguments):
    return {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5", "content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}]}}


def mcp_result(prompt_id, tool_id, text, is_error=False):
    return {"type": "user", "promptId": prompt_id, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": [{"type": "text", "text": text}], "is_error": is_error}]}}


def test_recordings_pair_each_mcp_call_with_its_result(write_session):
    (episode,) = episodes_from_session(write_session([
        human("check my note", "p1"),
        mcp_call("p1", "t1", "mcp__notes__get_note", {"id": "1"}), mcp_result("p1", "t1", "The note says ZEBRA"),
        mcp_call("p1", "t2", "mcp__claude_ai_Slack__slack_send_message", {"text": "hi"}),
        mcp_result("p1", "t2", "sent", is_error=False),
    ]))
    assert recordings_from_episode(episode) == {
        "notes": [{"tool": "get_note", "arguments": {"id": "1"}, "text": "The note says ZEBRA", "is_error": False}],
        "claude_ai_Slack": [{"tool": "slack_send_message", "arguments": {"text": "hi"}, "text": "sent", "is_error": False}],
    }


def test_the_stub_lists_recorded_tools_and_replays_only_recorded_calls():
    init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                  RECORDINGS["notes"])
    assert init["result"]["protocolVersion"] == "2025-06-18" and "tools" in init["result"]["capabilities"]
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, RECORDINGS["notes"]) is None
    listed = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, RECORDINGS["notes"])
    assert [tool["name"] for tool in listed["result"]["tools"]] == ["get_note"]
    hit = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "get_note", "arguments": {"id": "1"}}}, RECORDINGS["notes"])
    assert hit["result"] == {"content": [{"type": "text", "text": "The note says ZEBRA"}], "isError": False}
    miss = handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                   "params": {"name": "get_note", "arguments": {"id": "2"}}}, RECORDINGS["notes"])
    assert miss["result"]["isError"] is True and "no recorded result" in miss["result"]["content"][0]["text"]


def test_the_stub_speaks_newline_delimited_json_rpc_over_stdio(tmp_path):
    path = tmp_path / "recordings.json"
    path.write_text(json.dumps(RECORDINGS))
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_note", "arguments": {"id": "1"}}},
    ]
    done = subprocess.run([sys.executable, "-m", "culprit.stubs", "mcp", str(path), "notes"],
                          input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True, text=True,
                          timeout=30)
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[1]["result"]["content"][0]["text"] == "The note says ZEBRA"


# --- the answer host ---

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


def test_the_answer_stand_in_speaks_json_rpc_over_stdio(tmp_path):
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(ANSWERS))
    call = {"tool_name": "AskUserQuestion", "input": {"questions": [QUESTION]}}
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "answer", "arguments": call}}]
    done = subprocess.run([sys.executable, "-m", "culprit.stubs", "answers", str(path)],
                          input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True, text=True, timeout=30)
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["answer"]
    assert json.loads(replies[2]["result"]["content"][0]["text"])["behavior"] == "allow"
