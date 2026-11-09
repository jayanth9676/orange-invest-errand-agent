"""Adversarial stress and concurrency testing suite for EnterpriseLiveToolEnvironment.

Targets:
1. Concurrency: Rapid simultaneous hold_slot requests on identical provider and slot (TOCTOU).
2. Concurrency: Rapid simultaneous book requests and double-booking prevention (TOCTOU).
3. Idempotency: Duplicate idempotency key crash (sqlite3.IntegrityError) under concurrent execution.
4. Idempotency Destruction: Cancel and Reschedule overwriting the original booking creation idempotency key,
   breaking get_booking_status reconciliation.
5. Boundary: Hold TTL expiration boundary semantics (<= vs >).
6. State Machine: Invalid state transitions on book() (non-ACTIVE holds: CONSUMED, RELEASED).
7. State Machine: Invalid state transitions on reschedule() (CANCELLED, COMPLETED bookings).
8. State Machine: Invalid state transitions on cancel() (COMPLETED past bookings).
9. Concurrency: Concurrent rescheduling to the same target slot causing double booking.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
import pytest

from src.db import LocalDatabase
from src.live_tools import EnterpriseLiveToolEnvironment
from src.mock_tools import FaultConfig, ToolExecutionError


@pytest.fixture
def fresh_db(tmp_path):
    db_file = str(tmp_path / "test_adversarial_live.sqlite")
    db = LocalDatabase(db_file)
    db.seed_default_data(user_id="user_1", seed_50_memories=False)
    return db


# -----------------------------------------------------------------------------
# 1. State Machine: Invalid state transitions on book() (non-ACTIVE holds)
# -----------------------------------------------------------------------------
def test_book_rejects_already_consumed_or_released_hold(fresh_db):
    """
    DEFECT DEF-LIVE-01:
    A hold with status != 'ACTIVE' (e.g. 'CONSUMED', 'RELEASED') must NEVER be accepted by book().
    Currently, book() does not verify hold['status'] == 'ACTIVE'.
    """
    env = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_1")
    hold = env.hold_slot("prov_plumb_1", "2026-11-09 09:00", ttl_seconds=600)
    hold_id = hold["hold_id"]

    # 1. Book the hold normally
    b1 = env.book(hold_id, idempotency_key="idem_initial_book")
    assert b1["status"] == "BOOKED"

    # Status of hold in DB is now 'CONSUMED'
    db_hold = fresh_db.get_hold(hold_id)
    assert db_hold["status"] == "CONSUMED"

    # Now cancel booking b1, releasing the slot
    env.cancel(b1["booking_id"])

    # Attempt to book again using the already CONSUMED hold
    try:
        b2 = env.book(hold_id, idempotency_key="idem_reuse_consumed_hold")
        assert b2["status"] != "BOOKED", (
            f"SECURITY/INTEGRITY BUG: Allowed booking on already CONSUMED hold: {b2}"
        )
    except ToolExecutionError as exc:
        assert exc.code in ("invalid_hold", "hold_not_active")


def test_book_rejects_released_hold(fresh_db):
    """
    DEFECT DEF-LIVE-01 (Part 2):
    If a hold is explicitly RELEASED, book() must reject it.
    """
    env = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_1")
    hold = env.hold_slot("prov_plumb_1", "2026-11-09 09:00", ttl_seconds=600)
    hold_id = hold["hold_id"]

    # Explicitly release hold
    fresh_db.release_hold(hold_id)
    assert fresh_db.get_hold(hold_id)["status"] == "RELEASED"

    # Attempt to book the released hold
    try:
        b = env.book(hold_id, idempotency_key="idem_book_released")
        assert b["status"] != "BOOKED", (
            f"SECURITY BUG: Allowed booking on RELEASED hold: {b}"
        )
    except ToolExecutionError as exc:
        assert exc.code in ("invalid_hold", "hold_not_active")


# -----------------------------------------------------------------------------
# 2. State Machine: Invalid state transitions on reschedule() and cancel()
# -----------------------------------------------------------------------------
def test_reschedule_cancelled_booking_must_fail(fresh_db):
    """
    DEFECT DEF-LIVE-02:
    A CANCELLED booking cannot be rescheduled (revived into RESCHEDULED status).
    Currently, reschedule() does not check booking['status'] != 'CANCELLED'.
    """
    env = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_1")
    hold = env.hold_slot("prov_plumb_1", "2026-11-09 09:00", ttl_seconds=600)
    b = env.book(hold["hold_id"], idempotency_key="idem_for_cancel")
    booking_id = b["booking_id"]

    # Cancel booking
    c = env.cancel(booking_id)
    assert c["status"] == "CANCELLED"
    assert fresh_db.get_booking(booking_id)["status"] == "CANCELLED"

    # Attempt to reschedule CANCELLED booking
    try:
        r = env.reschedule(booking_id, new_slot="2026-11-09 11:00")
        db_b = fresh_db.get_booking(booking_id)
        assert db_b["status"] != "RESCHEDULED", (
            f"STATE MACHINE BUG: Cancelled booking revived to RESCHEDULED: {r}"
        )
    except ToolExecutionError as exc:
        assert exc.code in ("invalid_status", "booking_cancelled", "cannot_reschedule")


def test_reschedule_completed_booking_must_fail(fresh_db):
    """
    DEFECT DEF-LIVE-03:
    A COMPLETED booking (past service) cannot be rescheduled into the future.
    Currently, reschedule() does not check booking['status'] != 'COMPLETED'.
    """
    env = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_1")
    booking_id = "bk_past_elec_01"
    b_before = fresh_db.get_booking(booking_id)
    assert b_before["status"] == "COMPLETED"

    # Target an available, non-conflicting slot for prov_elec_1
    target_slot = "2026-11-09 08:30"
    try:
        r = env.reschedule(booking_id, new_slot=target_slot)
        db_b = fresh_db.get_booking(booking_id)
        assert db_b["status"] != "RESCHEDULED", (
            f"STATE MACHINE BUG: Past COMPLETED booking rescheduled to {target_slot}: {r}"
        )
    except ToolExecutionError as exc:
        assert exc.code in ("invalid_status", "booking_completed", "cannot_reschedule")


def test_cancel_completed_booking_must_fail(fresh_db):
    """
    DEFECT DEF-LIVE-04:
    A COMPLETED booking cannot be cancelled.
    Currently, cancel() does not check booking['status'] != 'COMPLETED'.
    """
    env = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_1")
    booking_id = "bk_past_elec_01"
    b_before = fresh_db.get_booking(booking_id)
    assert b_before["status"] == "COMPLETED"

    try:
        c = env.cancel(booking_id)
        db_b = fresh_db.get_booking(booking_id)
        assert db_b["status"] != "CANCELLED", (
            f"STATE MACHINE BUG: Past COMPLETED booking cancelled: {c}"
        )
    except ToolExecutionError as exc:
        assert exc.code in ("invalid_status", "booking_completed", "cannot_cancel")


# -----------------------------------------------------------------------------
# 3. Idempotency Destruction: Overwriting original creation idempotency key
# -----------------------------------------------------------------------------
def test_cancel_overwrites_original_idempotency_key(fresh_db):
    """
    DEFECT DEF-LIVE-05:
    When cancel() is called, update_booking_status overwrites bookings.idempotency_key
    with f'cancel_{booking_id}', destroying the original booking creation idempotency key.
    This causes get_booking_status(idempotency_key=original_key) to return NOT_FOUND.
    """
    env = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_1")
    hold = env.hold_slot("prov_plumb_1", "2026-11-09 09:00", ttl_seconds=600)
    original_idem_key = "idem_original_creation_key_999"

    # Create booking
    b = env.book(hold["hold_id"], idempotency_key=original_idem_key)
    booking_id = b["booking_id"]

    # Verify reconciliation by creation idempotency key works
    status_before = env.get_booking_status(idempotency_key=original_idem_key)
    assert status_before["status"] == "CONFIRMED"
    assert status_before["booking_id"] == booking_id

    # Now cancel the booking
    env.cancel(booking_id)

    # Reconciliation by original creation idempotency key MUST still locate the booking!
    status_after = env.get_booking_status(idempotency_key=original_idem_key)
    assert status_after["status"] != "NOT_FOUND", (
        f"IDEMPOTENCY CONTRACT BROKEN: Original booking creation key '{original_idem_key}' was erased "
        f"and overwritten on cancellation, returning NOT_FOUND: {status_after}"
    )
    assert status_after["status"] == "CANCELLED"


def test_reschedule_overwrites_original_idempotency_key(fresh_db):
    """
    DEFECT DEF-LIVE-06:
    When reschedule() is called, update_booking_slot overwrites bookings.idempotency_key
    with f'reschedule_{booking_id}_{new_slot}', destroying the original booking creation key.
    """
    env = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_1")
    hold = env.hold_slot("prov_plumb_1", "2026-11-09 09:00", ttl_seconds=600)
    original_idem_key = "idem_reschedule_audit_key_777"

    b = env.book(hold["hold_id"], idempotency_key=original_idem_key)
    booking_id = b["booking_id"]

    # Reschedule to another available slot
    env.reschedule(booking_id, new_slot="2026-11-09 11:00")

    # Reconciliation by original creation idempotency key MUST still locate the booking!
    status_after = env.get_booking_status(idempotency_key=original_idem_key)
    assert status_after["status"] != "NOT_FOUND", (
        f"IDEMPOTENCY CONTRACT BROKEN: Original creation key '{original_idem_key}' was erased "
        f"and overwritten on reschedule, returning NOT_FOUND: {status_after}"
    )
    assert status_after["status"] == "RESCHEDULED"


# -----------------------------------------------------------------------------
# 4. Concurrency: TOCTOU Race Conditions on hold_slot and book
# -----------------------------------------------------------------------------
def test_hold_slot_toctou_race_condition(fresh_db):
    """
    DEFECT DEF-LIVE-07:
    hold_slot() checks check_slot_conflict() and then executes INSERT INTO slot_holds.
    Because the SQLite lock is released between the SELECT check and the INSERT,
    and because slot_holds lacks a UNIQUE(provider_id, slot) constraint for active holds,
    two concurrent threads that check before either inserts will BOTH obtain active holds.
    """
    provider_id = "prov_plumb_1"
    slot = "2026-11-09 09:00"

    env1 = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_1")
    env2 = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_2")

    orig_check = fresh_db.check_slot_conflict
    barrier = threading.Barrier(2)

    def synchronized_check(*args, **kwargs):
        res = orig_check(*args, **kwargs)
        # Force both threads to synchronize right after check_slot_conflict returns
        try:
            barrier.wait(timeout=2.0)
        except Exception:
            pass
        return res

    fresh_db.check_slot_conflict = synchronized_check

    results = []
    errors = []

    def run_hold(env):
        try:
            r = env.hold_slot(provider_id, slot, ttl_seconds=600)
            results.append(r)
        except ToolExecutionError as e:
            errors.append(e)

    t1 = threading.Thread(target=run_hold, args=(env1,))
    t2 = threading.Thread(target=run_hold, args=(env2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    fresh_db.check_slot_conflict = orig_check

    active_holds = fresh_db.conn.execute(
        "SELECT * FROM slot_holds WHERE provider_id = ? AND slot = ? AND status = 'ACTIVE'",
        (provider_id, slot),
    ).fetchall()

    print(f"\n[TOCTOU HOLD] Holds granted: {len(results)}, Active in DB: {len(active_holds)}")
    assert len(active_holds) <= 1, (
        f"CONCURRENCY RACE DEFECT: Both threads obtained active holds for the same slot due to TOCTOU: "
        f"{[dict(h) for h in active_holds]}"
    )


def test_book_toctou_double_booking_race_condition(fresh_db):
    """
    DEFECT DEF-LIVE-08:
    book() queries conflicting bookings via SELECT, releases the DB lock, and then calls
    consume_hold_and_create_booking.
    Because bookings lacks a UNIQUE(provider_id, slot) constraint, two concurrent book()
    calls that pass the conflict SELECT check before either commits will BOTH succeed,
    creating a DOUBLE BOOKING.
    """
    provider_id = "prov_plumb_1"
    slot = "2026-11-09 11:00"

    now = time.time()
    fresh_db.conn.execute(
        "INSERT INTO slot_holds VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("hold_toctou_1", "user_1", provider_id, "plumber", slot, 1100, now + 600, "ACTIVE"),
    )
    fresh_db.conn.execute(
        "INSERT INTO slot_holds VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("hold_toctou_2", "user_2", provider_id, "plumber", slot, 1100, now + 600, "ACTIVE"),
    )
    fresh_db.conn.commit()

    env1 = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_1")
    env2 = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_2")

    orig_execute = fresh_db.conn.execute
    barrier = threading.Barrier(2)

    def synchronized_execute(sql, parameters=()):
        res = orig_execute(sql, parameters)
        # If this is the booking conflict check query in book(), synchronize threads
        if "SELECT booking_id FROM bookings WHERE provider_id = ? AND slot = ?" in sql:
            try:
                barrier.wait(timeout=2.0)
            except Exception:
                pass
        return res

    fresh_db.conn.execute = synchronized_execute

    results = []

    def run_book(env, hold_id, key):
        r = env.book(hold_id, idempotency_key=key)
        results.append(r)

    t1 = threading.Thread(target=run_book, args=(env1, "hold_toctou_1", "idem_t1"))
    t2 = threading.Thread(target=run_book, args=(env2, "hold_toctou_2", "idem_t2"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    fresh_db.conn.execute = orig_execute

    confirmed_bookings = fresh_db.conn.execute(
        "SELECT * FROM bookings WHERE provider_id = ? AND slot = ? AND status IN ('CONFIRMED', 'RESCHEDULED')",
        (provider_id, slot),
    ).fetchall()

    print(f"\n[TOCTOU BOOK] Results: {results}, Confirmed in DB: {len(confirmed_bookings)}")
    assert len(confirmed_bookings) <= 1, (
        f"DOUBLE BOOKING DEFECT: Both concurrent book calls confirmed for provider {provider_id} on slot {slot}: "
        f"{[dict(b) for b in confirmed_bookings]}"
    )


# -----------------------------------------------------------------------------
# 5. Concurrency: Duplicate Idempotency Key IntegrityError
# -----------------------------------------------------------------------------
def test_concurrent_identical_idempotency_key_integrity_error(fresh_db):
    """
    DEFECT DEF-LIVE-09:
    When two identical idempotency keys are sent concurrently, if both pass the initial
    get_booking_by_idempotency_key check before either commits, the second thread crashes
    with sqlite3.IntegrityError: UNIQUE constraint failed: bookings.idempotency_key
    instead of handling it and returning idempotent_replay: True.
    """
    provider_id = "prov_plumb_1"
    slot = "2026-11-10 10:00"
    user_id = "user_1"
    idem_key = "idem_race_integrity_hammer"

    env = EnterpriseLiveToolEnvironment(db=fresh_db, user_id=user_id)
    hold = env.hold_slot(provider_id, slot, ttl_seconds=600)

    orig_get_booking = fresh_db.get_booking_by_idempotency_key
    barrier = threading.Barrier(2)

    def synchronized_get(key):
        res = orig_get_booking(key)
        if key == idem_key:
            try:
                barrier.wait(timeout=2.0)
            except Exception:
                pass
        return res

    fresh_db.get_booking_by_idempotency_key = synchronized_get

    results = []
    crashes = []

    def call_book():
        try:
            r = env.book(hold["hold_id"], idempotency_key=idem_key)
            results.append(r)
        except Exception as e:
            crashes.append(e)

    t1 = threading.Thread(target=call_book)
    t2 = threading.Thread(target=call_book)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    fresh_db.get_booking_by_idempotency_key = orig_get_booking

    print(f"\n[IDEMPOTENCY CRASH] Successes: {len(results)}, Crashes: {len(crashes)}")
    if crashes:
        print(f"[IDEMPOTENCY CRASH EXCEPTION] {type(crashes[0])}: {crashes[0]}")

    assert len(crashes) == 0, (
        f"CRASH DEFECT: Concurrent duplicate idempotency key crashed with: {crashes}"
    )


# -----------------------------------------------------------------------------
# 6. Boundary: TTL Expiration Exact Boundary Consistency
# -----------------------------------------------------------------------------
def test_hold_ttl_exact_boundary_conditions(fresh_db):
    """
    Verify boundary behavior when now == expires_at_epoch and now > expires_at_epoch.
    """
    fault_cfg = FaultConfig()
    env = EnterpriseLiveToolEnvironment(db=fresh_db, fault_cfg=fault_cfg, user_id="user_1")

    hold = env.hold_slot("prov_plumb_1", "2026-11-09 09:00", ttl_seconds=10)
    expires_at = hold["expires_at_epoch"]

    base_time = env.now()
    offset_to_exact_expiry = expires_at - base_time
    fault_cfg.simulated_clock_offset_sec = offset_to_exact_expiry

    conflict_at_exact = fresh_db.check_slot_conflict("prov_plumb_1", "2026-11-09 09:00", now_ts=env.now())
    print(f"\n[BOUNDARY] At exact expiry: check_slot_conflict={conflict_at_exact}")

    # Advance 1 millisecond past TTL
    fault_cfg.simulated_clock_offset_sec = offset_to_exact_expiry + 0.001
    res = env.book(hold["hold_id"], idempotency_key="idem_boundary_01")
    assert res["status"] == "hold_expired", f"Expected hold_expired just past TTL boundary, got: {res}"
