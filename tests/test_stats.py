import math

import pytest

from culprit.stats import fisher_one_sided
from culprit.taxonomy import ORACLE_ORDER, Cause


def test_causes_are_the_spec_values_in_order():
    assert [c.value for c in Cause] == ["misjudged", "not-reproducible", "task", "environment", "agent-setup",
                                         "harness", "model", "multi-cause", "unexplained"]
    assert [c.value for c in ORACLE_ORDER] == ["task", "environment", "agent-setup", "harness", "model"]


@pytest.mark.parametrize("variant, baseline, expected", [
    (4, 0, 6 / 252),    # the spec's example: 4/5 against 0/5 clears p < 0.05
    (3, 0, 21 / 252),   # 3/5 against 0/5 does not
    (5, 0, 1 / 252),
    (5, 1, 6 / 252),
    (0, 0, 1.0),
])
def test_fisher_one_sided_matches_the_hypergeometric_tail(variant, baseline, expected):
    assert fisher_one_sided(variant, 5, baseline, 5) == pytest.approx(expected)


def test_fisher_one_sided_for_equal_rates_is_large():
    expected = sum(math.comb(4, k) * math.comb(6, 5 - k) for k in range(2, 5)) / math.comb(10, 5)
    assert fisher_one_sided(2, 5, 2, 5) == pytest.approx(expected)
    assert expected > 0.5
