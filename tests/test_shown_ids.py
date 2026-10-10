import json

import culprit.cli as cli
from culprit.batch import readiness_problem
from culprit.criterion import Criterion, save_criterion
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


def test_next_lines_and_status_rows_use_the_short_id(tmp_path, capsys):
    runs = tmp_path / "runs"
    queue(runs, A, B)
    assert cli.main(["confirm", A, "wrong-result"]) == 0
    err = capsys.readouterr().err
    assert "culprit criterion draft aaaaaaaa:bbbbbbbb" in err and A not in err
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "culprit criterion draft aaaaaaaa:bbbbbbbb" in out and "culprit confirm cccccccc:dddddddd" in out
    assert A not in out


def test_an_ambiguous_short_id_falls_back_to_the_full_id(tmp_path, capsys):
    runs = tmp_path / "runs"
    queue(runs, A, TWIN)
    assert cli.main(["confirm", A, "wrong-result"]) == 0
    assert f"culprit criterion draft {A}" in capsys.readouterr().err


def test_refusals_name_the_short_id(write_session, tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    session, prompt = A.split(":")
    write_session([snapshot("u-1", {}), human("fix a", prompt, session_id=session, cwd=str(repo))],
                  name=f"{session}.jsonl")
    runs = tmp_path / "runs"
    queue(runs, A, B)
    flags = []
    assert cli.main(["baseline", A, *flags]) == 3
    assert "culprit criterion draft aaaaaaaa:bbbbbbbb" in capsys.readouterr().err
    save_criterion(runs, A, Criterion("works", (), (), approved=True))
    assert cli.main(["baseline", A, *flags]) == 3
    assert "culprit restore aaaaaaaa:bbbbbbbb" in capsys.readouterr().err
    assert cli.main(["criterion", "draft", A, *flags]) == 1
    assert "culprit criterion show aaaaaaaa:bbbbbbbb" in capsys.readouterr().err
    (episode_dir(runs, A) / "pristine").mkdir()
    assert cli.main(["diagnose", A, *flags]) == 3
    assert "culprit confirm aaaaaaaa:bbbbbbbb" in capsys.readouterr().err
    assert "culprit confirm cccccccc:dddddddd" in readiness_problem(runs, B)


def test_status_rows_line_up_whatever_the_id_lengths(tmp_path, capsys):
    runs = tmp_path / "runs"
    queue(runs, A, TWIN, "s1:p1", B)
    assert cli.main(["status"]) == 0
    rows = capsys.readouterr().out.splitlines()
    assert len(rows) == 4
    assert len({row.index("next:") for row in rows}) == 1
    assert len({row.index("queued") for row in rows}) == 1
    assert rows[0].startswith(A) and rows[2].startswith("s1:p1 ") and rows[3].startswith("cccccccc:dddddddd ")


def test_triage_rows_show_the_short_id_and_json_keeps_the_full_id(write_session, tmp_path, capsys):
    from records import interrupt
    session, prompt = A.split(":")
    write_session([human("went wrong", prompt, session_id=session), interrupt(prompt)], name=f"{session}.jsonl")
    argv = ["triage"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "aaaaaaaa:bbbbbbbb" in out and A not in out
    assert cli.main([*argv, "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)["candidates"]
    assert row["episode_id"] == A and row["short_id"] == "aaaaaaaa:bbbbbbbb"


def test_triage_rows_fall_back_to_the_full_id_when_the_short_id_is_ambiguous(write_session, tmp_path, capsys):
    from records import interrupt
    for episode_id in (A, TWIN):
        session, prompt = episode_id.split(":")
        write_session([human("went wrong", prompt, session_id=session), interrupt(prompt)], name=f"{session}.jsonl")
    assert cli.main(["triage"]) == 0
    out = capsys.readouterr().out
    assert A in out and TWIN in out
