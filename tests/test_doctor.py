"""What `ontrak doctor` says about the one resource that fails quietly.

The rest of the report is about whether the range *can* do something. Capacity is
about whether it will still be doing it tomorrow: a storage pool that fills takes
Incus, the portal's database and its own containers down together, and the range
goes from working to "sign-in is not set up" with nothing in between — which is
exactly how this check came to exist. The verdict is a pure function so the
thresholds can be pinned here rather than read off a running host.
"""

from __future__ import annotations

from ontrak.cli import pool_verdict

GIB = 2**30


def test_a_pool_with_room_is_ok():
    state, line = pool_verdict(int(10 * GIB), int(100 * GIB))
    assert state == "ok"
    assert "90.0 GiB free" in line


def test_a_pool_above_ninety_percent_is_a_warning():
    state, line = pool_verdict(int(91 * GIB), int(100 * GIB))
    assert state == "warn"
    assert "91.0 GiB used of 100.0 GiB" in line
    assert "9.0 GiB free" in line


def test_a_pool_that_is_about_to_fill_is_a_failure():
    """97%, not 100: the last few percent are what the database and Docker need to
    keep writing, and by the time the pool reports full they have already stopped."""
    state, line = pool_verdict(int(98 * GIB), int(100 * GIB))
    assert state == "full"
    assert "portal's database" in line


def test_a_pool_that_reports_no_space_is_not_read_as_empty():
    """An unreadable total is `unknown`, never "0% used" — the driver may simply not
    report usage, and a range should not be told it is fine on no evidence."""
    assert pool_verdict(0, 0)[0] == "unknown"
