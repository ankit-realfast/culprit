from culprit.episodes import episodes_from_session, is_human_prompt, session_files
from records import assistant, human, interrupt, system, tool_result


def test_each_human_prompt_starts_an_episode_holding_everything_until_the_next(write_session):
    path = write_session([
        human("fix the bug", "p1"), assistant(), tool_result("p1"), system("turn_duration", durationMs=1000),
        human("now add a test", "p2"), assistant(),
    ])
    episodes = list(episodes_from_session(path))
    assert [e.prompt_text for e in episodes] == ["fix the bug", "now add a test"]
    assert len(episodes[0].records) == 4
    assert len(episodes[1].records) == 2


def test_an_interrupt_stays_inside_the_episode_it_interrupted(write_session):
    path = write_session([human("refactor x", "p1"), assistant(), interrupt("p1"), human("stop, do y", "p2")])
    episodes = list(episodes_from_session(path))
    assert [e.prompt_text for e in episodes] == ["refactor x", "stop, do y"]
    assert len(episodes[0].records) == 3


def test_injected_markup_is_not_a_human_prompt():
    for text in ["<task-notification>done</task-notification>", "<command-name>/clear</command-name>",
                 "<bash-input>ls</bash-input>", "  <local-command-stdout>x</local-command-stdout>"]:
        assert not is_human_prompt(human(text, "p"))


def test_tool_results_meta_records_and_empty_text_are_not_prompts():
    assert not is_human_prompt(tool_result("p"))
    assert not is_human_prompt(human("caveat text", "p", isMeta=True))
    assert not is_human_prompt(human("   ", "p"))
    assert not is_human_prompt(assistant())


def test_a_prompt_with_only_an_image_is_a_prompt():
    assert is_human_prompt(human([{"type": "image", "source": {}}], "p"))


def test_a_partial_last_line_is_skipped(write_session):
    path = write_session([human("fix the bug", "p1"), assistant()], trailing='{"type": "assist')
    episodes = list(episodes_from_session(path))
    assert len(episodes) == 1
    assert len(episodes[0].records) == 2


def test_a_session_with_no_human_prompt_has_no_episodes(write_session):
    path = write_session([system("turn_duration", durationMs=5), assistant()])
    assert list(episodes_from_session(path)) == []


def test_an_episode_records_its_id_place_mode_and_first_real_model(write_session):
    path = write_session([human("fix it", "p1", session_id="abc", permissionMode="auto"),
                          assistant(model="<synthetic>"), assistant(model="claude-opus-5")])
    (episode,) = episodes_from_session(path)
    assert episode.episode_id == "abc:p1"
    assert episode.prompt_uuid == "u-p1"
    assert (episode.cwd, episode.git_branch, episode.model) == ("/repo", "main", "claude-opus-5")
    assert episode.permission_mode == "auto"
    assert episode.session_file == path


def test_session_files_skip_subagent_logs(write_session, tmp_path):
    main_log = write_session([human("x", "p1")])
    subagent_dir = main_log.parent / "s1" / "subagents"
    subagent_dir.mkdir(parents=True)
    (subagent_dir / "agent-1.jsonl").write_text("{}\n")
    assert session_files(tmp_path / "projects") == [main_log]
