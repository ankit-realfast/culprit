"""The symptom axis of the spec's taxonomy: what went wrong in an episode, worst first."""

from enum import StrEnum


class Symptom(StrEnum):
    STOPPED_SHORT = "stopped-short"
    WRONG_RESULT = "wrong-result"
    IGNORED_INSTRUCTIONS = "ignored-instructions"
    WASTEFUL = "wasteful"
    NO_FAILURE = "no-failure"  # the human rejected the candidate: the spec's Misjudged check
