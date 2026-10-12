"""What each command prints for a person: one block in reading order, the episode named once at the top,
content before file paths, and one numbered Next section last. Synthetic records only."""

import json

import pytest

import culprit.cli as cli
from culprit.model import Criterion, load_criterion, save_criterion
from culprit.store import episode_dir, read_symptom
from fakes import FakeAsk
from records import human, snapshot

SESSION = "50c0a213-958f-4c4e-9d1a-0123456789ab"
PROMPT = "b0567c84-1111-2222-3333-444444444444"
EP = f"{SESSION}:{PROMPT}"
EXCERPT = "when anny filter is active @dashboard/people.js , view load|list gets hidden"
SUCCESS = ("On the dashboard people view, applying any filter keeps the list and its loading state visible: the "
           "list shows only the matching people, and clearing the filter brings every person back.")
DRAFT = {"success_means": SUCCESS, "checks": [],
         "must_not": ["Remove or disable the filter feature", "Change how the list looks with no filter active"]}
OTHERS = [f"{c * 8}-0000-0000-0000-000000000000:{d * 8}-0000-0000-0000-000000000000" for c, d in
          (("1", "2"), ("3", "4"), ("5", "6"))]


@pytest.fixture
def row4(write_session, tmp_path, monkeypatch):
    """The episode is row 4 of the queue; the store is runs/ in the current folder, as it is by default."""
    monkeypatch.setenv("CULPRIT_RUNS", "runs")
    monkeypatch.setenv("COLUMNS", "100")
    repo = tmp_path / "repo"
    repo.mkdir()
    write_session([snapshot(f"u-{PROMPT}", {}), human(EXCERPT, PROMPT, session_id=SESSION, cwd=str(repo)),
                   human("still hidden", "p2", session_id=SESSION, cwd=str(repo))], name=f"{SESSION}.jsonl")
    runs = tmp_path / "cwd" / "runs"
    runs.mkdir()
    rows = [{"score": 9, "summary": {"episode_id": other, "prompt_excerpt": "other"}} for other in OTHERS]
    rows.append({"score": 5, "summary": {"episode_id": EP, "prompt_excerpt": EXCERPT}})
    (runs / "candidates.json").write_text(json.dumps(rows))
    monkeypatch.setattr(cli, "ask_claude", FakeAsk(dict(DRAFT)))
    return runs


def terminal(monkeypatch, *answers):
    left = list(answers)
    monkeypatch.setattr(cli, "stdin_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": left.pop(0))
    return left


HEADER = f"""Episode 4 · 50c0a213:b0567c84
  "{EXCERPT}"
"""
RECAP = f"""What happened
  You asked      "{EXCERPT}"
  Claude did     no tool calls
  It ended       it finished
  You said next  "still hidden"
"""
DRAFT_BLOCK = """Draft success criterion — not approved yet
  Success means
    On the dashboard people view, applying any filter keeps the list and its loading state visible:
    the list shows only the matching people, and clearing the filter brings every person back.
  Must not
    1. Remove or disable the filter feature
    2. Change how the list looks with no filter active
  Checks
    none — add test commands (e.g. npm test) so replays are judged by tests
"""
APPROVED_BLOCK = DRAFT_BLOCK.replace("Draft success criterion — not approved yet", "Success criterion — approved")


def test_first_confirm_records_drafts_and_says_how_to_approve(row4, capsys):
    assert cli.main(["confirm", "4", "wrong-result"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"""{HEADER}
{RECAP}
✓ Recorded what went wrong: wrong-result

{DRAFT_BLOCK}
Next
  1. Read the draft. To change it, edit runs/50c0a213…b0567c84/criterion.json
  2. Approve it:  culprit confirm 4 approve
"""
    assert "next:" not in captured.err
    assert load_criterion(row4, EP).approved is False


def test_confirming_the_same_symptom_again_never_approves(row4, capsys):
    cli.main(["confirm", "4", "wrong-result"])
    capsys.readouterr()
    assert cli.main(["confirm", "4", "wrong-result"]) == 0
    out = capsys.readouterr().out
    assert "✓ What went wrong: wrong-result (recorded earlier)" in out and DRAFT_BLOCK in out
    assert "culprit confirm 4 approve" in out
    assert load_criterion(row4, EP).approved is False


def test_approve_in_the_symptom_position_approves_the_draft(row4, capsys):
    cli.main(["confirm", "4", "wrong-result"])
    capsys.readouterr()
    assert cli.main(["confirm", "4", "approve"]) == 0
    assert capsys.readouterr().out == f"""{HEADER}
✓ Approved the success criterion

{APPROVED_BLOCK}
Next
  1. Diagnose it:  culprit diagnose 4
"""
    assert load_criterion(row4, EP).approved is True
    assert read_symptom(row4, EP).value == "wrong-result"


def test_in_a_terminal_confirm_asks_and_approves(row4, capsys, monkeypatch):
    left = terminal(monkeypatch, "approve")
    assert cli.main(["confirm", "4", "wrong-result"]) == 0
    assert capsys.readouterr().out == f"""{HEADER}
{RECAP}
✓ Recorded what went wrong: wrong-result

{DRAFT_BLOCK}
✓ Approved the success criterion

Next
  1. Diagnose it:  culprit diagnose 4
"""
    assert left == [] and load_criterion(row4, EP).approved is True


def test_in_a_terminal_edit_first_leaves_the_draft_and_names_the_file(row4, capsys, monkeypatch):
    terminal(monkeypatch, "e")
    assert cli.main(["confirm", "4", "wrong-result"]) == 0
    out = capsys.readouterr().out
    assert out.endswith("""Next
  1. Edit runs/50c0a213…b0567c84/criterion.json
  2. Then read it again and approve it:  culprit confirm 4
""")
    assert load_criterion(row4, EP).approved is False


def test_in_a_terminal_confirm_with_no_symptom_shows_the_draft_and_asks(row4, capsys, monkeypatch):
    cli.main(["confirm", "4", "wrong-result"])
    capsys.readouterr()
    left = terminal(monkeypatch, "skip")
    assert cli.main(["confirm", "4"]) == 0
    out = capsys.readouterr().out
    assert DRAFT_BLOCK in out and left == [] and load_criterion(row4, EP).approved is False
    assert out.endswith("""Next
  1. Read the draft. To change it, edit runs/50c0a213…b0567c84/criterion.json
  2. Approve it:  culprit confirm 4 approve
""")


def test_in_a_terminal_confirm_with_no_symptom_asks_what_went_wrong(row4, capsys, monkeypatch):
    left = terminal(monkeypatch, "wasteful", "skip")
    assert cli.main(["confirm", "4"]) == 0
    assert left == [] and read_symptom(row4, EP).value == "wasteful"


def test_without_a_terminal_confirm_with_no_symptom_lists_the_symptoms(row4, capsys):
    assert cli.main(["confirm", "4"]) == 3
    err = capsys.readouterr().err
    assert "culprit confirm 4 <symptom>" in err and "wrong-result" in err and "no-failure" in err


def test_approve_refuses_without_a_symptom_or_a_draft(row4, capsys):
    assert cli.main(["confirm", "4", "approve"]) == 3
    assert "culprit confirm 4 <symptom>" in capsys.readouterr().err
    (episode_dir(row4, EP)).mkdir()
    (episode_dir(row4, EP) / "symptom.json").write_text(json.dumps({"episode_id": EP, "symptom": "wasteful"}))
    assert cli.main(["confirm", "4", "approve"]) == 3
    assert "no draft to approve" in capsys.readouterr().err and load_criterion(row4, EP) is None


def test_a_different_symptom_is_changed_and_the_draft_shown_again(row4, capsys):
    cli.main(["confirm", "4", "wrong-result"])
    capsys.readouterr()
    assert cli.main(["confirm", "4", "stopped-short"]) == 0
    out = capsys.readouterr().out
    assert "✓ Changed what went wrong: wrong-result → stopped-short" in out and DRAFT_BLOCK in out
    assert "culprit confirm 4 approve" in out and load_criterion(row4, EP).approved is False


def test_an_approved_criterion_says_so_and_names_diagnose(row4, capsys):
    cli.main(["confirm", "4", "wrong-result"])
    cli.main(["confirm", "4", "approve"])
    capsys.readouterr()
    assert cli.main(["confirm", "4", "wrong-result"]) == 0
    out = capsys.readouterr().out
    assert "✓ Success criterion already approved" in out
    assert out.endswith("Next\n  1. Diagnose it:  culprit diagnose 4\n")


def test_a_short_id_typed_is_reused_and_named_once(row4, capsys):
    assert cli.main(["confirm", "50c0a213:b0567c84", "wrong-result"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Episode 50c0a213:b0567c84\n")
    assert "culprit confirm 50c0a213:b0567c84 approve" in out and SESSION not in out


def test_a_full_id_typed_is_shortened_in_commands(row4, capsys):
    assert cli.main(["confirm", EP, "wrong-result"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Episode 50c0a213:b0567c84\n") and EP not in out
    assert "culprit confirm 50c0a213:b0567c84 approve" in out


def test_checks_are_listed_by_number(row4, capsys, monkeypatch):
    monkeypatch.setattr(cli, "ask_claude", FakeAsk({**DRAFT, "checks": ["npm test", "npm run lint"]}))
    cli.main(["confirm", "4", "wrong-result"])
    assert "  Checks\n    1. npm test\n    2. npm run lint\n" in capsys.readouterr().out


def diagnosed(runs):
    """Row 4 confirmed, approved, restored, baselined and diagnosed, from files as the pipeline writes them."""
    from culprit.diagnose import Verdict, save_verdict
    folder = episode_dir(runs, EP)
    (folder / "pristine").mkdir(parents=True)
    (folder / "symptom.json").write_text(json.dumps({"episode_id": EP, "symptom": "wrong-result"}))
    save_criterion(runs, EP, Criterion(SUCCESS, ("npm test",), tuple(DRAFT["must_not"]), approved=True))
    (folder / "restore.json").write_text(json.dumps({"snapshot_agreed": 3, "checked": 4, "unknown": 1,
                                                     "missing_backups": 0}))
    outcomes = {"baseline": {"variant": "baseline", "passes": 0, "runs": 5, "cost_usd": 1.5, "void": 0},
                "harness": {"variant": "harness", "passes": 4, "runs": 5, "cost_usd": 2.0, "void": 1}}
    for name, outcome in outcomes.items():
        (folder / "replays" / name).mkdir(parents=True)
        (folder / "replays" / name / "outcome.json").write_text(json.dumps(outcome))
        (folder / "replays" / name / "0").mkdir()
        (folder / "replays" / name / "0" / "result.json").write_text(json.dumps({"cost_usd": outcome["cost_usd"]}))
    verdict = Verdict(EP, "wrong-result", "harness", "harness passed 4/5 against the baseline's 0/5", outcomes,
                      {"environment": "no environment fix supplied (culprit env-fix)"})
    save_verdict(folder, verdict)
    return verdict


STATUS_DIAGNOSED = f"""{HEADER}
{RECAP}
Progress
  ✓ Queued
  ✓ What went wrong: wrong-result
  ✓ Success criterion drafted
  ✓ Success criterion approved
  ✓ Starting files restored
  ✓ Baseline replayed
  ✓ Diagnosed

Success criterion — approved
  Success means
    On the dashboard people view, applying any filter keeps the list and its loading state visible:
    the list shows only the matching people, and clearing the filter brings every person back.
  Must not
    1. Remove or disable the filter feature
    2. Change how the list looks with no filter active
  Checks
    1. npm test

Starting files
  The snapshot agrees with the episode's own record for 3 of 4 files; 1 unknown, 0 missing backups

Replays
  baseline: 0 of 5 passed, $1.50
  harness: 4 of 5 passed, 1 void, $2.00
  Spent so far: $3.50

Verdict
  Cause: harness
  Evidence
    harness passed 4/5 against the baseline's 0/5
    harness: baseline 0/5 → 4/5, p = 0.0238
  Not tested
    1. environment: no environment fix supplied (culprit env-fix)

Next
  1. Rank the changes across episodes:  culprit report
"""


def test_status_of_one_episode_reads_top_to_bottom(row4, capsys):
    diagnosed(row4)
    assert cli.main(["status", "4"]) == 0
    assert capsys.readouterr().out == STATUS_DIAGNOSED


def test_status_of_a_draft_marks_the_steps_done_and_names_approve(row4, capsys):
    cli.main(["confirm", "4", "wrong-result"])
    capsys.readouterr()
    assert cli.main(["status", "4"]) == 0
    assert capsys.readouterr().out == f"""{HEADER}
{RECAP}
Progress
  ✓ Queued
  ✓ What went wrong: wrong-result
  ✓ Success criterion drafted
    Success criterion approved
    Starting files restored
    Baseline replayed
    Diagnosed

{DRAFT_BLOCK}
Next
  1. Read the draft. To change it, edit runs/50c0a213…b0567c84/criterion.json
  2. Approve it:  culprit confirm 4 approve
"""


def test_status_lists_every_episode_then_trust_then_next(row4, capsys):
    cli.main(["confirm", "4", "wrong-result"])
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    assert capsys.readouterr().out == f"""Episodes
  ROW  EPISODE            STAGE              DO NEXT
    1  11111111:22222222  queued             culprit confirm 11111111:22222222 <symptom>
       "other"
    2  33333333:44444444  queued             culprit confirm 33333333:44444444 <symptom>
       "other"
    3  55555555:66666666  queued             culprit confirm 55555555:66666666 <symptom>
       "other"
    4  50c0a213:b0567c84  criterion drafted  culprit confirm 50c0a213:b0567c84
       "{EXCERPT}"

How far to trust the verdicts
  Judge: 0 of 0 labels agree, kappa not computable yet
    uncalibrated (needs 20 labels and kappa ≥ 0.6)
  Selftest: not run

Next
  1. Confirm the queue, episode by episode:  culprit confirm
  2. See one episode in full:  culprit status <row>
"""


def test_status_with_nothing_queued_names_triage(tmp_path, capsys):
    (tmp_path / "runs").mkdir()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Nothing queued yet.\n")
    assert out.endswith("Next\n  1. Rank likely failures:  culprit triage\n")


TRUST = """How far to trust the verdicts
  Judge: 0 of 0 labels agree, kappa not computable yet
    uncalibrated (needs 20 labels and kappa ≥ 0.6)
  Selftest: not run
"""


def test_diagnose_prints_the_verdict_then_trust_then_next(row4, capsys, monkeypatch):
    verdict = diagnosed(row4)
    monkeypatch.setattr(cli, "diagnose", lambda *args, **kwargs: verdict)
    assert cli.main(["diagnose", "4"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"""{HEADER}
Verdict
  Cause: harness
  Evidence
    harness passed 4/5 against the baseline's 0/5
    harness: baseline 0/5 → 4/5, p = 0.0238
  Not tested
    1. environment: no environment fix supplied (culprit env-fix)

{TRUST}
Next
  1. Rank the changes across episodes:  culprit report
  2. To test the environment too, store a script that fixes it:  culprit env-fix 4 <script>
"""
    assert "next:" not in captured.err and "up to" in captured.err  # the plan stays on stderr


def test_diagnose_refuses_an_unapproved_criterion_with_numbered_steps(row4, capsys):
    cli.main(["confirm", "4", "wrong-result"])
    capsys.readouterr()
    assert cli.main(["diagnose", "4"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == """culprit: episode 4 has no approved success criterion yet

Next
  1. Read the draft. To change it, edit runs/50c0a213…b0567c84/criterion.json
  2. Approve it:  culprit confirm 4 approve
"""


def test_env_fix_names_the_episode_once_and_ends_with_next(row4, capsys, tmp_path):
    diagnosed(row4)
    (episode_dir(row4, EP) / "verdict.json").unlink()
    script = tmp_path / "fix.sh"
    script.write_text("true\n")
    assert cli.main(["env-fix", "4", str(script)]) == 0
    assert capsys.readouterr().out == f"""{HEADER}
✓ Stored the environment fix in runs/50c0a213…b0567c84/env-fix.sh
  The environment oracle runs it in each replay's copy before Claude starts.

Next
  1. Diagnose it:  culprit diagnose 4
"""


def test_diagnose_with_no_id_summarises_then_names_the_report(row4, capsys, monkeypatch):
    from culprit.diagnose import BatchSummary
    diagnosed(row4)
    monkeypatch.setattr(cli, "diagnose_all", lambda *a, **k: BatchSummary(
        diagnosed=[EP], skipped={OTHERS[0]: "not confirmed — run `culprit confirm 11111111:22222222 <symptom>`"},
        over_cap={OTHERS[1]: 3900.0}, stopped_at=OTHERS[2], spent_usd=88.5))
    assert cli.main(["diagnose"]) == 0
    assert capsys.readouterr().out == f"""Diagnosed
  1. 50c0a213:b0567c84  harness: baseline 0/5 → 4/5, p = 0.0238

Skipped
  1. 11111111:22222222  not confirmed — run `culprit confirm 11111111:22222222 <symptom>`

Stopped at a spend cap
  1. 33333333:44444444  hit its $3900.00 cap; its finished runs are kept
  2. 55555555:66666666  stopped here: the $78000.00 total for this command is spent

Spent $88.50

{TRUST}
Next
  1. Read the report, saved in runs/report.md:  culprit report
  2. Carry on from where the total ran out:  culprit diagnose
"""


def test_triage_has_a_header_row_a_stage_column_and_ends_with_next(write_session, tmp_path, capsys, monkeypatch):
    from records import interrupt
    monkeypatch.setenv("CULPRIT_RUNS", "runs")
    write_session([human("first thing", "p1", session_id="s1"), interrupt("p1"),
                   human("second thing", "p2", session_id="s1"), interrupt("p2")])
    assert cli.main(["triage"]) == 0
    capsys.readouterr()
    runs = tmp_path / "cwd" / "runs"
    (episode_dir(runs, "s1:p2")).mkdir()
    (episode_dir(runs, "s1:p2") / "symptom.json").write_text(json.dumps({"episode_id": "s1:p2", "symptom": "wasteful"}))
    assert cli.main(["triage"]) == 0
    lines = capsys.readouterr().out.splitlines()
    header, end = lines[0], lines.index("")
    assert header.split() == ["ROW", "SCORE", "EPISODE", "STAGE", "EVIDENCE", "PROMPT"]
    first, second = [line for line in lines[1:end] if line.split()[0].isdigit()]
    for sub_line in (line for line in lines[1:end] if not line.split()[0].isdigit()):
        assert sub_line.index("not replayable:") == header.index("EPISODE")  # under its row's episode
    rows = {line.split()[2]: line for line in (first, second)}
    assert rows["s1:p2"][header.index("STAGE"):].startswith("confirmed ")
    assert rows["s1:p1"][header.index("STAGE"):header.index("EVIDENCE")].strip() == ""
    for row in (first, second):
        assert row[header.index("EVIDENCE"):].startswith("interrupted")
        assert row[header.index("PROMPT") - 2:header.index("PROMPT")] == "  "
    assert lines[end:] == ["", "Scanned 2 episodes; the 2 shown are the whole queue",
                         "Saved the queue in runs/candidates.json",
                         "", "Next",
                         "  1. Say what went wrong and approve what success means, row by row:  culprit confirm",
                         "  2. Or one row at a time:  culprit confirm <row> <symptom>"]


def test_triage_marks_each_stage_an_episode_has_reached(tmp_path):
    from culprit.report import EpisodeStatus
    from culprit.diagnose import Verdict
    marks = [cli.stage_mark(EpisodeStatus("a:1", stage, "")) for stage in
             ("not queued", "queued", "confirmed", "criterion drafted", "criterion approved", "restored", "baselined")]
    assert marks == ["", "", "confirmed", "draft", "approved", "restored", "baselined"]
    verdict = Verdict("a:1", "wrong-result", "harness", "e", {}, {})
    assert cli.stage_mark(EpisodeStatus("a:1", "diagnosed", "", verdict=verdict)) == "diagnosed: harness"
