# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""fixtures.history.compare(): tolerance as a ratio, slack as an absolute floor. The cases are
the two metrics that failed on rotating boards on bravo, 2026-10-17."""
import pytest

from fixtures.history import compare

pytestmark = pytest.mark.smoke


def test_without_slack_a_ratio_past_tolerance_fails():
    assert compare(0.0391, 0.0244, "ms")[0]          # M4 sleep_accuracy, one step, x1.60


def test_slack_absorbs_a_small_absolute_change():
    assert not compare(0.0391, 0.0244, "ms", slack=1)[0]
    assert not compare(0.0366, 0.00268, "s", slack=0.25)[0]   # RP2350 mount_s, x13.6


def test_slack_still_fails_a_real_regression():
    assert compare(1.5, 0.0244, "ms", slack=1)[0]
    assert compare(0.40, 0.003, "s", slack=0.25)[0]


def test_within_tolerance_passes_whatever_the_slack():
    assert not compare(1.2, 1.0)[0]


def test_higher_is_better():
    assert compare(50, 100, better="higher")[0]
    assert not compare(50, 100, better="higher", slack=60)[0]
    assert not compare(90, 100, better="higher")[0]


def test_detail_names_the_slack():
    assert compare(0.0391, 0.0244, "ms", slack=1)[1] == \
        "0.0391 ms vs baseline 0.0244 (x1.60), slack 1 ms"
    assert "slack" not in compare(2, 1)[1]
