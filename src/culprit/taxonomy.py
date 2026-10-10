"""The symptom axis of the spec's taxonomy: what went wrong in an episode, worst first."""

from enum import StrEnum


class Symptom(StrEnum):
    STOPPED_SHORT = "stopped-short"
    WRONG_RESULT = "wrong-result"
    IGNORED_INSTRUCTIONS = "ignored-instructions"
    WASTEFUL = "wasteful"
    NO_FAILURE = "no-failure"  # the human rejected the candidate: the spec's Misjudged check


class Cause(StrEnum):
    """The cause axis: what to change, cheapest fix first; the first cause proven wins."""

    MISJUDGED = "misjudged"
    NOT_REPRODUCIBLE = "not-reproducible"
    TASK = "task"
    ENVIRONMENT = "environment"
    AGENT_SETUP = "agent-setup"
    HARNESS = "harness"
    MODEL = "model"
    MULTI_CAUSE = "multi-cause"
    UNEXPLAINED = "unexplained"


ORACLE_ORDER = (Cause.TASK, Cause.ENVIRONMENT, Cause.AGENT_SETUP, Cause.HARNESS, Cause.MODEL)
