import json

import culprit.cli as cli
from culprit.diagnose import readiness_problem
from culprit.model import Criterion, save_criterion
from culprit.store import display_id, episode_dir
from records import human, snapshot

A = "aaaaaaaa-1111-2222-3333-444444444444:bbbbbbbb-5555-6666-7777-888888888888"
TWIN = "aaaaaaaa-9999-2222-3333-444444444444:bbbbbbbb-0000-6666-7777-888888888888"  # same short id as A
B = "cccccccc-1111-2222-3333-444444444444:dddddddd-5555-6666-7777-888888888888"


def queue(runs, *ids):
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "candidates.json").write_text(json.dumps([{"summary": {"episode_id": i}, "score": 1} for i in ids]))


def test_the_short_id_is_shown_only_when_it_finds_that_episode_alone(tmp_path):
    runs = tmp_path / "runs"
    queue(runs, A, B)
    assert display_id(runs, A) == "aaaaaaaa:bbbbbbbb"
    queue(runs, A, TWIN, B)
    assert display_id(runs, A) == A
    assert display_id(runs, "eeeeeeee-1:ffffffff-2") == "eeeeeeee-1:ffffffff-2"  # unknown: nothing to expand from


def logged(write_session, tmp_path, *ids):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    for episode_id in ids:
        session, prompt = episode_id.split(":")
        write_session([snapshot("u-1", {}), human("fix a", prompt, session_id=session, cwd=str(repo))],
                      name=f"{session}.jsonl")


def test_next_lines_and_status_rows_use_the_short_id(write_session, tmp_path, capsys):
    runs = tmp_path / "runs"
    queue(runs, A, B)
    logged(write_session, tmp_path, A)
    assert cli.main(["confirm", A, "wrong-result"]) == 0
    out = capsys.readouterr().out
    assert "culprit confirm aaaaaaaa:bbbbbbbb approve" in out and A not in out
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "culprit confirm aaaaaaaa:bbbbbbbb\n" in out and "culprit confirm cccccccc:dddddddd <symptom>" in out
    assert A not in out


def test_an_ambiguous_short_id_falls_back_to_the_full_id(write_session, tmp_path, capsys):
    runs = tmp_path / "runs"
    queue(runs, A, TWIN)
    logged(write_session, tmp_path, A)
    assert cli.main(["confirm", A, "wrong-result"]) == 0
    assert f"culprit confirm {A} approve" in capsys.readouterr().out


def test_refusals_name_the_short_id(write_session, tmp_path, capsys):
    logged(write_session, tmp_path, A)
    runs = tmp_path / "runs"
    queue(runs, A, B)
    assert cli.main(["diagnose", A]) == 3
    assert "culprit confirm aaaaaaaa:bbbbbbbb <symptom>" in capsys.readouterr().err
    (episode_dir(runs, A)).mkdir()
    (episode_dir(runs, A) / "symptom.json").write_text(json.dumps({"episode_id": A, "symptom": "wrong-result"}))
    assert cli.main(["diagnose", A]) == 3
    assert "culprit confirm aaaaaaaa:bbbbbbbb wrong-result" in capsys.readouterr().err
    save_criterion(runs, A, Criterion("works", (), (), approved=True))
    assert cli.main(["diagnose", A]) == 1  # the log has no snapshot for this prompt, so restoring first refuses
    assert "aaaaaaaa:bbbbbbbb cannot be replayed" in capsys.readouterr().err
    assert "culprit confirm cccccccc:dddddddd" in readiness_problem(runs, B)


def test_status_rows_line_up_whatever_the_id_lengths(tmp_path, capsys):
    runs = tmp_path / "runs"
    queue(runs, A, TWIN, "s1:p1", B)
    assert cli.main(["status"]) == 0
    rows = capsys.readouterr().out.split("\n\n")[0].splitlines()[1:]  # the heading, the column names, the rows
    assert len(rows) == 5
    assert len({row.index("Stage" if i == 0 else "queued") for i, row in enumerate(rows)}) == 1
    assert len({row.index("Do next" if i == 0 else "culprit") for i, row in enumerate(rows)}) == 1
    assert A in rows[1] and "s1:p1 " in rows[3] and "cccccccc:dddddddd " in rows[4]


def test_triage_rows_show_the_short_id_and_the_queue_keeps_the_full_id(write_session, tmp_path, capsys):
    from records import interrupt
    session, prompt = A.split(":")
    write_session([human("went wrong", prompt, session_id=session), interrupt(prompt)], name=f"{session}.jsonl")
    argv = ["triage"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "aaaaaaaa:bbbbbbbb" in out and A not in out
    (row,) = json.loads((tmp_path / "runs" / "candidates.json").read_text())
    assert row["summary"]["episode_id"] == A


def test_triage_rows_fall_back_to_the_full_id_when_the_short_id_is_ambiguous(write_session, tmp_path, capsys):
    from records import interrupt
    for episode_id in (A, TWIN):
        session, prompt = episode_id.split(":")
        write_session([human("went wrong", prompt, session_id=session), interrupt(prompt)], name=f"{session}.jsonl")
    assert cli.main(["triage"]) == 0
    out = capsys.readouterr().out
    assert A in out and TWIN in out
