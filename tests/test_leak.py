from culprit.leak import LeakCheck, check_leak, leak_prompt
from fakes import FakeAsk


def test_the_prompt_carries_the_draft_the_original_and_the_later_messages():
    prompt = leak_prompt("Fix login; empty passwords must be rejected.", "fix login",
                         ["no — the bug is in validate() in auth.py"])
    for expected in ["Fix login; empty passwords must be rejected.", "fix login", "validate() in auth.py",
                     "Stating the outcome the person wanted is allowed"]:
        assert expected in prompt


def test_the_answer_becomes_a_leak_check():
    ask = FakeAsk({"leaks": True, "evidence": "names validate() in auth.py"})
    assert check_leak("Fix validate() in auth.py", "fix login", ["it's validate() in auth.py"], ask=ask) == \
        LeakCheck(True, "names validate() in auth.py")
    assert ask.calls[0][1]["required"] == ["leaks", "evidence"]
