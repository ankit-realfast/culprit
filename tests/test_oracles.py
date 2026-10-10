from dataclasses import replace

from culprit.model import Criterion
from culprit.logs import episodes_from_session
from culprit.oracles import MAX_CONTEXT_CHARS, ORACLE_MODEL, VariantBuild, build_variant, combine, harness_context
from culprit.replay import ReplaySettings, settings_from_json, settings_to_json
from culprit.store import Cause, Symptom
from fakes import FakeAsk
from records import human, tool_use

CRITERION = Criterion("empty passwords are rejected with a message", (), ("weaken validation",), approved=True)
BASE = ReplaySettings(prompt="fix login", model="claude-opus-5", permission_mode="auto", max_budget_usd=2.0)


def episode_with(write_session, repo, *uses):
    (episode,) = episodes_from_session(write_session([human("fix login", "p1", cwd=str(repo)), *uses]))
    return episode


def build(cause, episode, tmp_path, ask=None, env_script=None, base=BASE):
    return build_variant(cause, base, episode=episode, follow_ups=["still lets empty passwords in"],
                         criterion=CRITERION, symptom=Symptom.WRONG_RESULT, pristine=tmp_path / "pristine",
                         env_script=env_script, ask=ask or FakeAsk())


def test_settings_round_trip_through_json(tmp_path):
    settings = replace(BASE, disallowed_tools=("WebFetch",), setup_script=tmp_path / "fix.sh")
    assert settings_from_json(settings_to_json(settings)) == settings


def test_the_task_oracle_rewrites_only_the_prompt_after_a_clean_leak_check(write_session, tmp_path):
    ask = FakeAsk({"text": "Fix login so empty passwords are rejected with a message."},
                  {"leaks": False, "evidence": "none"})
    built = build(Cause.TASK, episode_with(write_session, tmp_path), tmp_path, ask=ask)
    assert built == VariantBuild(replace(BASE, prompt="Fix login so empty passwords are rejected with a message."), None)
    assert "empty passwords are rejected" in ask.calls[0][0]
    assert "still lets empty passwords in" in ask.calls[1][0]


def test_a_leaked_draft_is_redrafted_with_the_evidence(write_session, tmp_path):
    ask = FakeAsk({"text": "Fix validate() in auth.py"}, {"leaks": True, "evidence": "names validate()"},
                  {"text": "Fix login so empty passwords are rejected."}, {"leaks": False, "evidence": "none"})
    built = build(Cause.TASK, episode_with(write_session, tmp_path), tmp_path, ask=ask)
    assert built.settings.prompt == "Fix login so empty passwords are rejected."
    assert "names validate()" in ask.calls[2][0]


def test_two_leaks_make_the_cause_untested(write_session, tmp_path):
    ask = FakeAsk({"text": "a"}, {"leaks": True, "evidence": "x"}, {"text": "b"}, {"leaks": True, "evidence": "y"})
    built = build(Cause.AGENT_SETUP, episode_with(write_session, tmp_path), tmp_path, ask=ask)
    assert built.settings is None
    assert "leak" in built.untested_reason


def test_the_agent_setup_oracle_replaces_only_the_appended_instructions(write_session, tmp_path):
    ask = FakeAsk({"text": "Verify every change against its stated outcome before reporting done."},
                  {"leaks": False, "evidence": "none"})
    built = build(Cause.AGENT_SETUP, episode_with(write_session, tmp_path), tmp_path, ask=ask)
    assert built.settings == replace(BASE, append_system_prompt="Verify every change against its stated outcome before reporting done.")
    assert "CLAUDE.md" in ask.calls[0][0]


def test_the_environment_oracle_needs_a_human_script(write_session, tmp_path):
    episode = episode_with(write_session, tmp_path)
    assert build(Cause.ENVIRONMENT, episode, tmp_path).untested_reason.startswith("no environment fix")
    script = tmp_path / "env-fix.sh"
    script.write_text("true\n")
    assert build(Cause.ENVIRONMENT, episode, tmp_path, env_script=script).settings == replace(BASE, setup_script=script)


def test_the_model_oracle_swaps_in_fable(write_session, tmp_path):
    assert build(Cause.MODEL, episode_with(write_session, tmp_path), tmp_path).settings == replace(BASE, model=ORACLE_MODEL)


def test_the_model_oracle_is_untested_for_an_episode_already_on_fable(write_session, tmp_path):
    built = build(Cause.MODEL, episode_with(write_session, tmp_path), tmp_path, base=replace(BASE, model="claude-fable-5"))
    assert built.settings is None
    assert "already ran on Fable" in built.untested_reason


def test_the_harness_oracle_lifts_restrictions_preloads_files_and_doubles_the_cap(write_session, tmp_path):
    repo = tmp_path / "repo"
    pristine = tmp_path / "pristine"
    (pristine / "src").mkdir(parents=True)
    (pristine / "src" / "auth.py").write_text("def validate(): ...\n")
    episode = episode_with(write_session, repo, tool_use("Read", file_path=str(repo / "src" / "auth.py")),
                           tool_use("Edit", file_path="src/auth.py"), tool_use("Read", file_path="/etc/hosts"))
    base = replace(BASE, tools="Read", disallowed_tools=("Edit",))
    built = build_variant(Cause.HARNESS, base, episode=episode, follow_ups=[], criterion=CRITERION,
                          symptom=Symptom.WRONG_RESULT, pristine=pristine, env_script=None, ask=FakeAsk())
    s = built.settings
    assert (s.tools, s.disallowed_tools, s.max_budget_usd, s.prompt) == ("default", (), 4.0, "fix login")
    assert s.context_preamble.count("=== src/auth.py ===") == 1
    assert "def validate()" in s.context_preamble
    assert "hosts" not in s.context_preamble


def test_harness_context_stops_at_the_cap(write_session, tmp_path):
    repo = tmp_path / "repo"
    pristine = tmp_path / "pristine"
    pristine.mkdir()
    (pristine / "big.txt").write_text("y" * (MAX_CONTEXT_CHARS + 10))
    (pristine / "small.txt").write_text("small\n")
    episode = episode_with(write_session, repo, tool_use("Read", file_path="big.txt"),
                           tool_use("Read", file_path="small.txt"))
    context = harness_context(episode, pristine)
    assert "big.txt" not in context and "small.txt" in context


def test_combine_takes_each_causes_fields_from_its_variant():
    combined = combine(BASE, {Cause.TASK: replace(BASE, prompt="clear"), Cause.MODEL: replace(BASE, model=ORACLE_MODEL),
                              Cause.HARNESS: replace(BASE, tools="default", context_preamble="FILES", max_budget_usd=4.0)})
    assert (combined.prompt, combined.model, combined.tools, combined.context_preamble, combined.max_budget_usd) == \
        ("clear", ORACLE_MODEL, "default", "FILES", 4.0)
    assert combined.append_system_prompt is None
