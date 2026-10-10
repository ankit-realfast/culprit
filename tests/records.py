"""Builders for synthetic Claude Code log records. Tests never use real session content."""


def human(text, prompt_id, session_id="s1", **extra):
    record = {
        "type": "user",
        "sessionId": session_id,
        "promptId": prompt_id,
        "uuid": f"u-{prompt_id}",
        "timestamp": "2026-10-01T10:00:00Z",
        "cwd": "/repo",
        "gitBranch": "main",
        "message": {"role": "user", "content": text},
    }
    record.update(extra)
    return record


def assistant(model="claude-opus-5", text="ok"):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "model": model, "content": [{"type": "text", "text": text}]},
    }


def tool_result(prompt_id, is_error=False):
    return {
        "type": "user",
        "promptId": prompt_id,
        "toolUseResult": {},
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x", "is_error": is_error}],
        },
    }


def interrupt(prompt_id):
    return {
        "type": "user",
        "promptId": prompt_id,
        "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]},
    }


def system(subtype, **extra):
    record = {"type": "system", "subtype": subtype}
    record.update(extra)
    return record


def tool_use(name, **tool_input):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "model": "claude-opus-5",
                    "content": [{"type": "tool_use", "id": "t1", "name": name, "input": tool_input}]},
    }


def edit_result(prompt_id, file_path, original_file):
    return {
        "type": "user",
        "promptId": prompt_id,
        "toolUseResult": {"filePath": file_path, "oldString": "a", "newString": "b",
                          "originalFile": original_file, "replaceAll": False},
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    }


def snapshot(message_id, tracked):
    """tracked maps a path to a backup file name, or to None for a file that did not exist yet."""
    return {
        "type": "file-history-snapshot",
        "messageId": message_id,
        "isSnapshotUpdate": False,
        "snapshot": {
            "messageId": message_id,
            "timestamp": "2026-10-01T10:00:00Z",
            "trackedFileBackups": {
                path: {"backupFileName": name, "version": 1, "backupTime": "2026-10-01T10:00:00Z"}
                for path, name in tracked.items()
            },
        },
    }


def write_result(prompt_id, file_path, original_file=None, kind="create"):
    """A Write tool result; kind "create" means the file did not exist before it."""
    return {
        "type": "user",
        "promptId": prompt_id,
        "toolUseResult": {"type": kind, "filePath": file_path, "content": "new", "originalFile": original_file},
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    }

