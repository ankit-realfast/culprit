import pytest

from culprit.logs import EpisodeNotFound, episodes_from_session, find_episode, follow_up_prompts, tool_uses
from records import assistant, human, tool_use


def test_tool_uses_lists_every_tool_call_in_order(write_session):
    path = write_session([human("x", "p1"), tool_use("Read", file_path="/repo/a.py"),
                          assistant(), tool_use("Edit", file_path="/repo/a.py")])
    (episode,) = episodes_from_session(path)
    assert [(t["name"], t["input"]["file_path"]) for t in tool_uses(episode)] == [
        ("Read", "/repo/a.py"), ("Edit", "/repo/a.py")]


def test_find_episode_returns_the_matching_episode(write_session, tmp_path):
    write_session([human("first", "p1"), human("second", "p2")], name="s1.jsonl")
    episode = find_episode(tmp_path / "projects", "s1:p2")
    assert episode.prompt_text == "second"


def test_an_unknown_session_is_reported(tmp_path):
    (tmp_path / "projects").mkdir()
    with pytest.raises(EpisodeNotFound, match="nope.jsonl"):
        find_episode(tmp_path / "projects", "nope:p1")


def test_an_unknown_prompt_in_a_known_session_is_reported(write_session, tmp_path):
    write_session([human("first", "p1")], name="s1.jsonl")
    with pytest.raises(EpisodeNotFound, match="s1:p9"):
        find_episode(tmp_path / "projects", "s1:p9")


def test_follow_ups_are_the_next_human_prompts_up_to_count(write_session, tmp_path):
    write_session([human("a", "p1"), human("b", "p2"), human("c", "p3"), human("d", "p4"), human("e", "p5")],
                  name="s1.jsonl")
    assert follow_up_prompts(tmp_path / "projects", "s1:p2", count=2) == ["c", "d"]
    assert follow_up_prompts(tmp_path / "projects", "s1:p5") == []
