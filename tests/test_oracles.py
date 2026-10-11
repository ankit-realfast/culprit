import json
from dataclasses import replace
from pathlib import Path

from culprit.diagnose import MAX_CONTEXT_CHARS, ORACLE_MODEL, VariantBuild, build_variant, combine, harness_context
from culprit.logs import Episode, episodes_from_session
from culprit.model import Criterion
from culprit.replay import ReplaySettings, settings_from_json, settings_to_json
from culprit.store import Cause, Symptom
from fakes import FakeAsk
from records import assistant, human, tool_use

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


def pristine_repo(tmp_path):
    """A restored starting copy with the project's own instructions, beside a live folder edited since."""
    pristine = tmp_path / "pristine"
    for path, text in {"CLAUDE.md": "Run uv run pytest before reporting done.\n",
                       ".claude/CLAUDE.md": "Prefer small commits.\n",
                       "src/app.py": "", "src/deep/inner.py": "", "docs/guide.md": "", "README.md": "",
                       ".git/HEAD": "ref: refs/heads/main\n", "node_modules/left-pad/index.js": ""}.items():
        (pristine / path).parent.mkdir(parents=True, exist_ok=True)
        (pristine / path).write_text(text)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("A rule added after the episode.\n")
    return pristine, repo


def agent_setup_prompt(write_session, tmp_path, pristine, repo, *records):
    ask = FakeAsk({"text": "Verify before reporting."}, {"leaks": False, "evidence": "none"})
    build_variant(Cause.AGENT_SETUP, BASE, episode=episode_with(write_session, repo, *records), follow_ups=[],
                  criterion=CRITERION, symptom=Symptom.WRONG_RESULT, pristine=pristine, env_script=None, ask=ask)
    return ask.calls[0][0]


def test_the_agent_setup_drafter_sees_the_instructions_and_layout_the_agent_started_with(
        write_session, tmp_path, monkeypatch):
    import culprit.diagnose as diagnose
    pristine, repo = pristine_repo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("Ancestor: never touch vendor code.\n")
    user_file = tmp_path / "home" / ".claude" / "CLAUDE.md"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("User: answer in plain English.\n")
    monkeypatch.setattr(diagnose, "USER_CLAUDE_DIR", user_file.parent)
    prompt = agent_setup_prompt(write_session, tmp_path, pristine, repo,
                                assistant(text="I found the bug in DURING_EPISODE_DETAIL"),
                                tool_use("Read", file_path="src/app.py"))
    for text in ("Run uv run pytest before reporting done.", "Prefer small commits.",
                 "Ancestor: never touch vendor code.", "User: answer in plain English."):
        assert text in prompt
    assert prompt.index("User: answer") < prompt.index("Ancestor:") < prompt.index("Run uv run pytest")
    for entry in ("src/", "src/app.py", "src/deep/", "docs/guide.md", "README.md"):
        assert entry in prompt
    assert "inner.py" not in prompt  # third level
    assert ".git" not in prompt and "left-pad" not in prompt
    assert "added after the episode" not in prompt  # the project's own files come from the starting copy
    assert "DURING_EPISODE_DETAIL" not in prompt
    assert "rather than repeat" in prompt
    assert "12 lines" in prompt


def test_the_agent_setup_drafter_context_stays_within_its_caps(write_session, tmp_path):
    from culprit.diagnose import MAX_INSTRUCTIONS_CHARS, MAX_LAYOUT_ENTRIES
    pristine, repo = pristine_repo(tmp_path)
    (pristine / "CLAUDE.md").write_text("z" * (MAX_INSTRUCTIONS_CHARS + 500))
    for i in range(MAX_LAYOUT_ENTRIES + 50):
        (pristine / f"f{i:04}.txt").write_text("")
    prompt = agent_setup_prompt(write_session, tmp_path, pristine, repo)
    assert MAX_INSTRUCTIONS_CHARS - 100 < prompt.count("z") <= MAX_INSTRUCTIONS_CHARS
    listed = [line for line in prompt.splitlines() if line.startswith("f0") and line.endswith(".txt")]
    assert len(listed) < MAX_LAYOUT_ENTRIES
    assert "more not shown" in prompt


def test_the_agent_setup_drafter_says_when_no_instructions_exist(write_session, tmp_path):
    pristine = tmp_path / "pristine"
    (pristine / "main.py").parent.mkdir(parents=True)
    (pristine / "main.py").write_text("")
    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = agent_setup_prompt(write_session, tmp_path, pristine, repo)
    assert "no CLAUDE.md" in prompt and "main.py" in prompt


def test_instructions_come_from_every_ancestor_up_to_the_root_with_their_imports(tmp_path, monkeypatch):
    import culprit.diagnose as diagnose
    from culprit.diagnose import existing_instructions
    root = tmp_path / "root"
    cwd = root / "a" / "b" / "c" / "repo"
    pristine = tmp_path / "pristine"
    files = {
        tmp_path / "CLAUDE.md": "above the root",
        root / "CLAUDE.md": "root rule @shared/rules.md",
        root / "shared" / "rules.md": "shared rule @../CLAUDE.md",  # imports its importer back: a cycle
        root / "a" / ".claude" / "CLAUDE.md": "a dotclaude rule",
        root / "a" / "b" / "CLAUDE.local.md": "b local rule",
        cwd / "CLAUDE.md": "live project file, edited after the episode",
        pristine / "CLAUDE.md": "project rule @docs/style.md\n```\n@ignored.md\n```",
        pristine / "docs" / "style.md": "style rule @../../outside.md",
        cwd.parent / "outside.md": "outside rule",  # imported from inside the project, read from disk
        pristine / "ignored.md": "must not load",
        tmp_path / "policy" / "CLAUDE.md": "policy rule",
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    monkeypatch.setattr(diagnose, "ANCESTOR_ROOT", root)
    monkeypatch.setattr(diagnose, "MANAGED_INSTRUCTIONS", (tmp_path / "policy" / "CLAUDE.md",))
    episode = Episode("s", "p", None, tmp_path / "none.jsonl", "fix", "", str(cwd), None, None, None, ())
    texts = [text for _, text in existing_instructions(episode, pristine)]
    assert texts == ["policy rule", "root rule @shared/rules.md", "shared rule @../CLAUDE.md", "a dotclaude rule",
                     "b local rule", "project rule @docs/style.md\n```\n@ignored.md\n```",
                     "style rule @../../outside.md", "outside rule"]


def test_the_instruction_cap_keeps_the_files_nearest_the_working_directory(tmp_path):
    from culprit.diagnose import MAX_INSTRUCTIONS_CHARS, existing_instructions
    cwd = tmp_path / "repo"
    pristine = tmp_path / "pristine"
    pristine.mkdir()
    (pristine / "CLAUDE.md").write_text("project rule")
    (tmp_path / "CLAUDE.md").write_text("a" * (MAX_INSTRUCTIONS_CHARS * 2))
    episode = Episode("s", "p", None, tmp_path / "none.jsonl", "fix", "", str(cwd), None, None, None, ())
    shown = existing_instructions(episode, pristine)
    assert shown[-1] == ("CLAUDE.md", "project rule")
    assert sum(len(text) for _, text in shown) == MAX_INSTRUCTIONS_CHARS


def write_files(files):
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def instructions_for(tmp_path, cwd, pristine):
    from culprit.diagnose import existing_instructions
    episode = Episode("s", "p", None, tmp_path / "none.jsonl", "fix", "", str(cwd), None, None, None, ())
    return existing_instructions(episode, pristine)


def test_launch_time_rules_and_policy_settings_load_but_on_demand_files_do_not(tmp_path, monkeypatch):
    import culprit.diagnose as diagnose
    from culprit.diagnose import MAX_INSTRUCTION_FILE_BYTES
    home, cwd, pristine = tmp_path / "home" / ".claude", tmp_path / "repo", tmp_path / "pristine"
    write_files({
        home / "CLAUDE.md": "user rule",
        home / "rules" / "general.md": "user general rule",
        home / "rules" / "scoped.md": "---\npaths:\n  - src/**\n---\nuser scoped rule",
        pristine / "CLAUDE.md": "project rule",
        pristine / ".claude" / "rules" / "testing.md": "---\ndescription: x\n---\nproject testing rule\npaths: y",
        pristine / ".claude" / "rules" / "deep" / "api.md": "---\npaths: [\"api/**\"]\n---\nproject api rule",
        pristine / "sub" / "CLAUDE.md": "subfolder rule",  # loads on demand, during the episode
        pristine / "AGENTS.md": "agents rule",  # a CLAUDE.md exists, so AGENTS.md is not read
        tmp_path / "policy" / "managed-settings.json": json.dumps({"claudeMd": "policy setting rule"}),
    })
    (tmp_path / "CLAUDE.local.md").write_text("x" * (MAX_INSTRUCTION_FILE_BYTES + 1))  # too big: skipped
    monkeypatch.setattr(diagnose, "USER_CLAUDE_DIR", home)
    monkeypatch.setattr(diagnose, "MANAGED_SETTINGS", (tmp_path / "policy" / "managed-settings.json",))
    texts = [text for _, text in instructions_for(tmp_path, cwd, pristine)]
    assert texts == ["policy setting rule", "user rule", "user general rule", "project rule",
                     "---\ndescription: x\n---\nproject testing rule\npaths: y"]


def test_claude_md_excludes_globs_skip_matching_files(tmp_path, monkeypatch):
    import culprit.diagnose as diagnose
    cwd, pristine = tmp_path / "team" / "repo", tmp_path / "pristine"
    write_files({tmp_path / "team" / "CLAUDE.md": "team rule", pristine / "CLAUDE.md": "project rule",
                 pristine / ".claude" / "settings.local.json": json.dumps({"claudeMdExcludes": [f"{tmp_path}/team/*"]})})
    assert [text for _, text in instructions_for(tmp_path, cwd, pristine)] == ["project rule"]
    write_files({tmp_path / "home" / ".claude" / "settings.json": json.dumps({"claudeMdExcludes": ["**/repo/CLAUDE.md"]})})
    monkeypatch.setattr(diagnose, "USER_CLAUDE_DIR", tmp_path / "home" / ".claude")
    assert [text for _, text in instructions_for(tmp_path, cwd, pristine)] == []


def test_agents_md_is_read_only_where_no_claude_md_exists(tmp_path):
    cwd, pristine = tmp_path / "team" / "repo", tmp_path / "pristine"
    write_files({tmp_path / "team" / "AGENTS.md": "team agents rule", pristine / "AGENTS.md": "project agents rule"})
    assert [text for _, text in instructions_for(tmp_path, cwd, pristine)] == ["team agents rule",
                                                                               "project agents rule"]
    (tmp_path / "CLAUDE.md").write_text("a claude rule")
    assert [text for _, text in instructions_for(tmp_path, cwd, pristine)] == ["a claude rule"]


def task_prompt(write_session, *records, which=-1):
    episode = list(episodes_from_session(write_session(list(records))))[which]
    ask = FakeAsk({"text": "Fix login."}, {"leaks": False, "evidence": "none"})
    build_variant(Cause.TASK, BASE, episode=episode, follow_ups=[], criterion=CRITERION,
                  symptom=Symptom.WRONG_RESULT, pristine=Path("/nonexistent"), env_script=None, ask=ask)
    return ask.calls[0][0]


def test_the_task_drafter_sees_the_earlier_conversation_of_a_follow_up(write_session):
    tool_output = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "TOOL_OUTPUT_TEXT"}]}}
    prompt = task_prompt(write_session, human("set up the login form", "p1"), assistant(text="Form added."),
                         tool_use("Read", file_path="EARLIER_TOOL_INPUT.py"), tool_output,
                         human("now make it reject empty passwords", "p2"), assistant(text="DURING_EPISODE"),
                         human("AFTER_EPISODE", "p3"), which=1)
    assert "set up the login form" in prompt and "Form added." in prompt
    assert prompt.index("set up the login form") < prompt.index("Form added.") < prompt.index("now make it")
    assert "TOOL_OUTPUT_TEXT" not in prompt and "EARLIER_TOOL_INPUT" not in prompt
    assert "DURING_EPISODE" not in prompt and "AFTER_EPISODE" not in prompt
    assert "already has" in prompt


def test_the_earlier_conversation_is_trimmed_from_the_start(write_session):
    from culprit.diagnose import MAX_HISTORY_CHARS
    turns = []
    for i in range(30):
        turns += [human(f"request {i:02} " + "Z" * (MAX_HISTORY_CHARS // 20), f"e{i}"), assistant(text=f"reply {i:02}")]
    prompt = task_prompt(write_session, *turns, human("now finish it", "last"))
    assert "reply 29" in prompt and "request 29" in prompt
    assert "request 00" not in prompt
    assert prompt.count("Z") <= MAX_HISTORY_CHARS


def test_a_first_prompt_has_no_conversation_section(write_session):
    prompt = task_prompt(write_session, human("fix login", "p1"), assistant(text="done"), human("thanks", "p2"),
                         which=0)
    assert "<conversation>" not in prompt and "done" not in prompt
