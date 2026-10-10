import subprocess


class FakeAsk:
    """Stands in for culprit.model.ask_claude: records each prompt, returns canned answers in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, prompt, schema):
        self.calls.append((prompt, schema))
        return self.responses.pop(0)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRun:
    """Stands in for subprocess.run in replay tests: answers claude, checks, setup scripts and diff."""

    def __init__(self, claude_stdout="", check_codes=None, diff="", on_claude=None, claude_raises=None,
                 setup_code=0):
        self.claude_stdout = claude_stdout
        self.check_codes = check_codes or {}
        self.diff = diff
        self.on_claude = on_claude
        self.claude_raises = claude_raises
        self.setup_code = setup_code
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[0] == "claude":
            if self.on_claude:
                self.on_claude(kwargs["cwd"])
            if self.claude_raises:
                raise self.claude_raises
            return completed(self.claude_stdout)
        if command[:2] == ["bash", "-c"]:
            return completed("check output", returncode=self.check_codes.get(command[2], 0))
        if command[0] == "bash":
            return completed("", stderr="setup failed" if self.setup_code else "", returncode=self.setup_code)
        if command[0] == "diff":
            return completed(self.diff, returncode=1 if self.diff else 0)
        raise AssertionError(f"unexpected command {command}")
