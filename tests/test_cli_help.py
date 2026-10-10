import pytest

import culprit.cli as cli

GROUPS = {
    "workflow": ["triage", "env-fix", "diagnose", "report", "status"],
    "human review": ["confirm"],
    "trust": ["review", "selftest"],
}


def help_text(capsys, *argv):
    with pytest.raises(SystemExit) as stopped:
        cli.main([*argv, "--help"])
    assert stopped.value.code == 0
    return capsys.readouterr().out


def test_top_level_help_groups_every_command_and_walks_through_a_run(capsys):
    text = help_text(capsys)
    positions = {group: text.index(f"{group}:") for group in GROUPS}
    assert list(positions) == sorted(positions, key=positions.get)
    bounds = sorted(positions.values()) + [len(text)]
    for group, start in positions.items():
        section = text[start:bounds[bounds.index(start) + 1]]
        for command in GROUPS[group]:
            assert f"\n  {command} " in section, (group, command)
    assert "example run:" in text and "culprit diagnose #1" in text


def test_every_command_is_in_exactly_one_group():
    parser = cli.build_parser()
    commands = next(a for a in parser._actions if a.dest == "command").choices
    assert sorted(commands) == sorted(c for group in GROUPS.values() for c in group)


@pytest.mark.parametrize("command", [c for group in GROUPS.values() for c in group])
def test_each_command_shows_examples(capsys, command):
    text = help_text(capsys, command)
    examples = text[text.index("examples:"):]
    assert f"culprit {command}" in examples
