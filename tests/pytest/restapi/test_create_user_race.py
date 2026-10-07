# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Regression test for the first-admin bootstrap TOCTOU race.

Before the fix, N concurrent POST /api/create-user calls on an empty database
all passed the User.query.first() check and all committed with role=admin.
The fix serializes the bootstrap branch with a threading.Lock, re-checks
inside the lock, and writes a UNIQUE sentinel row in the same transaction
as the first user. This test fires N concurrent requests and asserts exactly
one admin lands.
"""

import threading

from webserver import restapi


def _race_create_user(app, n: int) -> list[tuple[str, int]]:
    """Fire ``n`` concurrent create-user POSTs, released by a shared barrier.

    Each thread owns its own test client. The barrier lines every thread up
    before the simultaneous release so the contention window is as tight as
    the host can produce.
    """
    results: list[tuple[str, int]] = []
    results_lock = threading.Lock()
    release = threading.Barrier(n)

    def post(index: int) -> None:
        username = f"racer{index}"
        client = app.test_client()
        release.wait(timeout=10)
        resp = client.post(
            "/api/create-user",
            json={"username": username, "password": "race-test-pw-12"},
        )
        with results_lock:
            results.append((username, resp.status_code))

    threads = [threading.Thread(target=post, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_bootstrap_creates_exactly_one_admin(app):
    """Five concurrent POSTs on an empty DB produce one admin and four refusals."""
    try:
        results = _race_create_user(app, n=5)

        successes = [r for r in results if r[1] == 201]
        refusals = [r for r in results if r[1] != 201]

        assert len(successes) == 1, (
            f"expected exactly one 201, got {len(successes)} out of {len(results)}: {results}"
        )
        # Losing racers see "a user exists" inside the lock and return 401. A
        # lock bypass that reached the commit would land on IntegrityError
        # and also 401.
        for username, status in refusals:
            assert status == 401, f"losing racer {username} got {status}, expected 401"

        with app.app_context():
            users = restapi.User.query.all()
            assert len(users) == 1
            assert users[0].role == restapi.ADMIN_ROLE
            markers = restapi.BootstrapMarker.query.all()
            assert len(markers) == 1, "sentinel row must land in the same transaction"
    finally:
        # The worker threads each opened a pooled connection; dispose the pool
        # so no stale schema-inspection cache survives into the next test.
        with app.app_context():
            restapi.db.engine.dispose()


def test_bootstrap_sentinel_blocks_a_second_bootstrap_after_user_deletion(app):
    """Even if the user table is wiped later, the sentinel keeps bootstrap closed.

    This exercises the belt-and-suspenders path: the lock's re-check reads BOTH
    User and BootstrapMarker, so a device whose users were deleted outside the
    API cannot be re-bootstrapped without authentication.
    """
    client = app.test_client()
    first = client.post(
        "/api/create-user",
        json={"username": "founder", "password": "pw-abcd-1234"},
    )
    assert first.status_code == 201, first.get_json()

    with app.app_context():
        restapi.User.query.delete()
        restapi.db.session.commit()
        assert restapi.User.query.count() == 0
        assert restapi.BootstrapMarker.query.count() == 1

    second = client.post(
        "/api/create-user",
        json={"username": "pretender", "password": "pw-abcd-1234"},
    )
    assert second.status_code == 401
