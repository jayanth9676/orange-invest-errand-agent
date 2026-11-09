"""Empirical Adversarial Concurrency and Stress Verification Harness.

Authored by challenger_m1_stress.
Validates:
1. Multi-threaded race conditions on identical slots (only 1 can succeed).
2. High-concurrency interleaved connector queries and persistent cache writes.
3. Rapid concurrent bookings, holds, and cancellations under thread contention.
4. Database integrity and invariant enforcement under high load.
"""

import concurrent.futures
import os
import random
import sqlite3
import time
from unittest.mock import MagicMock
import httpx
import pytest

from src.connectors.context7_connector import Context7Connector
from src.connectors.exa_connector import ExaToolConnector
from src.db import LocalDatabase
from src.live_tools import EnterpriseLiveToolEnvironment
from src.mock_tools import ToolExecutionError


@pytest.fixture
def stress_db(tmp_path):
    """Isolated SQLite database for stress testing."""
    db_file = str(tmp_path / "empirical_stress_test.sqlite")
    db = LocalDatabase(db_file)
    db.seed_default_data(user_id="user_stress_main", seed_50_memories=False)
    return db


def test_concurrent_slot_hold_race_condition(stress_db):
    """Stress test: 20 threads simultaneously race to hold the exact same provider slot.
    
    Invariant: Exactly 1 thread must succeed. 19 threads must receive slot_unavailable.
    Zero unhandled SQLite errors, zero crashes, zero deadlocks.
    """
    env = EnterpriseLiveToolEnvironment(db=stress_db, user_id="user_race_1")
    providers = env.search_providers("plumber", "Indiranagar")
    prov_id = providers["providers"][0]["provider_id"]
    target_slot = providers["providers"][0]["available_slots"][0]

    num_threads = 20
    results = []
    errors = []

    def attempt_hold(thread_id: int):
        thread_env = EnterpriseLiveToolEnvironment(db=stress_db, user_id=f"user_thread_{thread_id}")
        try:
            hold = thread_env.hold_slot(prov_id, target_slot, ttl_seconds=300)
            results.append((thread_id, hold))
        except ToolExecutionError as e:
            errors.append((thread_id, e.code))
        except Exception as e:
            errors.append((thread_id, f"UNEXPECTED_EXCEPTION: {type(e).__name__}: {e}"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(attempt_hold, i) for i in range(num_threads)]
        concurrent.futures.wait(futures)

    # Invariants:
    # 1. Exactly 1 thread succeeded
    assert len(results) == 1, f"Expected exactly 1 successful hold, got {len(results)}"
    # 2. 19 threads got slot_unavailable
    assert len(errors) == num_threads - 1
    for thread_id, code in errors:
        assert code == "slot_unavailable", f"Unexpected error code from thread {thread_id}: {code}"

    # 3. Check SQLite integrity
    row = stress_db.conn.execute("PRAGMA integrity_check;").fetchone()
    assert row[0] == "ok"


def test_heavy_concurrent_connectors_and_tool_mutations(stress_db):
    """Stress test: 30 concurrent workers interleaving Exa cache writes, Context7 queries,
    holds, books, cancellations, and reschedules for 100+ operations.
    """
    mock_http = MagicMock(spec=httpx.Client)
    mock_http.post.side_effect = httpx.ConnectError("Offline simulated network")

    exa = ExaToolConnector(db_conn=stress_db.conn, client=mock_http)
    c7 = Context7Connector(db_conn=stress_db.conn, client=mock_http)

    num_workers = 25
    operations_per_worker = 10
    total_exceptions = []

    def worker_lifecycle(worker_id: int):
        try:
            env = EnterpriseLiveToolEnvironment(db=stress_db, user_id=f"worker_user_{worker_id}")
            for op in range(operations_per_worker):
                # 1. Connector operations
                cache_key = f"exa_stress_{worker_id}_{op}"
                exa._set_cached(cache_key, "exa_search", {"w": worker_id, "op": op})
                cached_data = exa._get_cached(cache_key)
                assert cached_data["op"] == op

                # Context7 lookup
                c7.resolve_appliance_context("Air Conditioner", "not cooling")

                # 2. Search & booking lifecycle
                res = env.search_providers("electrician", "Koramangala")
                assert res["count"] > 0
                prov = res["providers"][0]
                prov_id = prov["provider_id"]
                slots = prov["available_slots"]
                slot_to_try = slots[op % len(slots)]

                # Attempt hold
                try:
                    hold = env.hold_slot(prov_id, slot_to_try, ttl_seconds=60)
                    # Attempt book
                    book_res = env.book(hold["hold_id"], f"idem_stress_{worker_id}_{op}")
                    bk_id = book_res["booking_id"]

                    # Idempotent re-book
                    re_book = env.book(hold["hold_id"], f"idem_stress_{worker_id}_{op}")
                    assert re_book["booking_id"] == bk_id

                    # Reschedule or Cancel
                    if op % 2 == 0:
                        env.cancel(bk_id)
                    else:
                        alt_slot = slots[(op + 1) % len(slots)]
                        env.reschedule(bk_id, alt_slot)

                except ToolExecutionError as e:
                    # Expected business errors under slot contention
                    assert e.code in ("slot_unavailable", "HOLD_NOT_FOUND", "HOLD_NOT_OWNED")

        except Exception as ex:
            total_exceptions.append((worker_id, type(ex).__name__, str(ex)))

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(worker_lifecycle, w) for w in range(num_workers)]
        concurrent.futures.wait(futures)

    # Invariants:
    # 1. No unexpected Python or SQLite crashes (no DatabaseLocked, no threading deadlocks)
    assert len(total_exceptions) == 0, f"Unexpected exceptions encountered: {total_exceptions}"

    # 2. Check SQLite integrity check
    row = stress_db.conn.execute("PRAGMA integrity_check;").fetchone()
    assert row[0] == "ok"

    # 3. Check for any duplicate active bookings across the entire database
    duplicates = stress_db.conn.execute("""
        SELECT provider_id, slot, COUNT(*) as cnt 
        FROM bookings 
        WHERE status IN ('CONFIRMED', 'RESCHEDULED')
        GROUP BY provider_id, slot
        HAVING cnt > 1
    """).fetchall()
    assert len(duplicates) == 0, f"Integrity invariant violated! Duplicate active bookings: {duplicates}"


def test_concurrent_idempotent_booking_race(stress_db):
    """Stress test: 10 threads simultaneously call book() with the EXACT SAME idempotency key.
    
    Invariant: Exactly one atomic insertion occurs; all 10 calls succeed and return the
    identical booking_id with status='BOOKED' and idempotent_replay handling.
    """
    env = EnterpriseLiveToolEnvironment(db=stress_db, user_id="user_idem_race")
    providers = env.search_providers("plumber", "Indiranagar")
    prov_id = providers["providers"][0]["provider_id"]
    slot = providers["providers"][0]["available_slots"][0]

    hold = env.hold_slot(prov_id, slot)
    shared_idem_key = "shared_concurrent_idem_key_999"

    num_threads = 10
    results = []
    errors = []

    def book_worker(thread_id: int):
        try:
            worker_env = EnterpriseLiveToolEnvironment(db=stress_db, user_id="user_idem_race")
            res = worker_env.book(hold["hold_id"], shared_idem_key)
            results.append((thread_id, res))
        except Exception as e:
            errors.append((thread_id, type(e).__name__, str(e)))

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(book_worker, i) for i in range(num_threads)]
        concurrent.futures.wait(futures)

    # Invariants:
    assert len(errors) == 0, f"Unexpected errors during idempotent book race: {errors}"
    assert len(results) == num_threads

    booking_ids = {r["booking_id"] for _, r in results}
    assert len(booking_ids) == 1, f"Expected exactly one unique booking ID, got: {booking_ids}"
    assert results[0][1]["status"] == "BOOKED"

    # Exactly 1 row in bookings table with this idempotency key
    rows = stress_db.conn.execute(
        "SELECT COUNT(*) as cnt FROM bookings WHERE idempotency_key = ?",
        (shared_idem_key,)
    ).fetchone()
    assert rows["cnt"] == 1


def test_multithreaded_mixed_read_write_starvation_and_sweeps(stress_db):
    """Stress test: 15 readers performing rapid search queries while 15 writers are creating
    holds and running expire_stale_holds sweeps. Validates that SQLite WAL mode and proxy lock
    prevent reader starvation and deadlocks.
    """
    mock_http = MagicMock(spec=httpx.Client)
    mock_http.post.side_effect = httpx.ConnectError("Offline simulation")

    exa = ExaToolConnector(db_conn=stress_db.conn, client=mock_http)
    num_readers = 15
    num_writers = 15
    rounds = 10

    reader_successes = []
    writer_successes = []
    fatal_errors = []

    def reader_task(r_id: int):
        try:
            env = EnterpriseLiveToolEnvironment(db=stress_db, user_id=f"reader_{r_id}")
            for i in range(rounds):
                # Search providers
                res = env.search_providers("plumber", "Indiranagar")
                assert res["count"] > 0
                # Cache lookup
                exa._get_cached(f"cache_key_shared_{i % 5}")
                reader_successes.append(1)
        except Exception as e:
            fatal_errors.append(("reader", r_id, type(e).__name__, str(e)))

    def writer_task(w_id: int):
        try:
            env = EnterpriseLiveToolEnvironment(db=stress_db, user_id=f"writer_{w_id}")
            providers = env.search_providers("electrician", "Koramangala")
            prov_id = providers["providers"][0]["provider_id"]
            slots = providers["providers"][0]["available_slots"]
            for i in range(rounds):
                # Write to cache
                exa._set_cached(f"cache_key_shared_{i % 5}", "exa_search", {"data": i, "w": w_id})
                # Attempt hold (may fail with slot_unavailable if another writer grabbed it)
                slot = slots[i % len(slots)]
                try:
                    h = env.hold_slot(prov_id, slot, ttl_seconds=1)
                except ToolExecutionError as te:
                    assert te.code == "slot_unavailable"
                # Run stale sweep
                stress_db.expire_stale_holds(now_ts=time.time() + 5.0)
                writer_successes.append(1)
        except Exception as e:
            fatal_errors.append(("writer", w_id, type(e).__name__, str(e)))

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_readers + num_writers) as executor:
        f_readers = [executor.submit(reader_task, r) for r in range(num_readers)]
        f_writers = [executor.submit(writer_task, w) for w in range(num_writers)]
        concurrent.futures.wait(f_readers + f_writers)

    assert len(fatal_errors) == 0, f"Fatal concurrency errors: {fatal_errors}"
    assert len(reader_successes) == num_readers * rounds
    assert len(writer_successes) == num_writers * rounds

    # Verify SQLite integrity
    row = stress_db.conn.execute("PRAGMA integrity_check;").fetchone()
    assert row[0] == "ok"

