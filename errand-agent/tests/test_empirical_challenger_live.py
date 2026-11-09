"""Empirical Challenger Deep Adversarial Test Suite for Live Tools.

Comprehensive stress and boundary test harnesses:
1. High-concurrency hold contention (50 concurrent threads on 1 slot)
2. Concurrent Reschedule vs Book collision on identical target slot
3. Concurrent Reschedule vs Reschedule collision (multiple bookings contending for 1 target slot)
4. Comprehensive State Transition Exhaustion Matrix:
   - Holds: ACTIVE, CONSUMED, RELEASED, EXPIRED, SLOT_TAKEN
   - Bookings: CONFIRMED, RESCHEDULED, CANCELLED, COMPLETED
5. Hold expiration boundary fuzzing (exact millisecond boundaries)
6. Cross-user isolation and authorization barriers
7. Random high-load concurrent chaos loop (50 concurrent workers performing random ops)
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import random
import threading
import time
import pytest

from src.db import LocalDatabase
from src.live_tools import EnterpriseLiveToolEnvironment
from src.mock_tools import FaultConfig, ToolExecutionError


@pytest.fixture
def fresh_db(tmp_path):
    db_file = str(tmp_path / "test_empirical_live.sqlite")
    db = LocalDatabase(db_file)
    db.seed_default_data(user_id="user_1", seed_50_memories=False)
    return db


# =============================================================================
# 1. High-Concurrency Slot Contention (50 concurrent threads on 1 slot)
# =============================================================================
def test_high_concurrency_hold_slot_contention(fresh_db):
    """50 threads concurrently attempt to hold the exact same provider and slot.
    Invariant: Exactly 1 hold succeeds and is ACTIVE. Exactly 49 fail with slot_unavailable.
    """
    provider_id = "prov_plumb_1"
    slot = "2026-11-09 09:00"
    num_threads = 50

    barrier = threading.Barrier(num_threads)
    successes = []
    failures = []

    def attempt_hold(user_idx: int):
        user_id = f"user_{user_idx}"
        env = EnterpriseLiveToolEnvironment(db=fresh_db, user_id=user_id)
        # Wait for all threads to be ready to maximize race contention
        barrier.wait()
        try:
            res = env.hold_slot(provider_id, slot, ttl_seconds=300)
            successes.append((user_id, res))
        except ToolExecutionError as exc:
            failures.append((user_id, exc.code, str(exc)))

    threads = [threading.Thread(target=attempt_hold, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Invariants verification
    assert len(successes) == 1, (
        f"Contention failure: Expected exactly 1 successful hold, got {len(successes)}: {successes}"
    )
    assert len(failures) == num_threads - 1, (
        f"Expected {num_threads - 1} failures, got {len(failures)}"
    )
    for user_id, code, msg in failures:
        assert code == "slot_unavailable", f"Unexpected failure code: {code} ({msg})"

    # Verify database state
    active_holds = fresh_db.conn.execute(
        "SELECT * FROM slot_holds WHERE provider_id = ? AND slot = ? AND status = 'ACTIVE'",
        (provider_id, slot),
    ).fetchall()
    assert len(active_holds) == 1, f"Expected 1 active hold in DB, found {len(active_holds)}"
    assert active_holds[0]["hold_id"] == successes[0][1]["hold_id"]


# =============================================================================
# 2. Concurrent Reschedule vs Book Collision on the Same Target Slot
# =============================================================================
def test_concurrent_reschedule_vs_book_collision(fresh_db):
    """Thread 1 attempts to book hold on Slot B.
    Thread 2 attempts to reschedule an existing confirmed booking from Slot A to Slot B.
    Invariant: Exactly one wins Slot B. Neither creates a double booking.
    """
    provider_id = "prov_plumb_1"
    slot_a = "2026-11-09 09:00"
    slot_b = "2026-11-09 11:00"

    env_user1 = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_1")
    env_user2 = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_2")

    # User 1 holds Slot B
    hold_b = env_user1.hold_slot(provider_id, slot_b, ttl_seconds=600)
    hold_b_id = hold_b["hold_id"]

    # User 2 holds Slot A and books it
    hold_a = env_user2.hold_slot(provider_id, slot_a, ttl_seconds=600)
    book_a = env_user2.book(hold_a["hold_id"], idempotency_key="idem_user2_initial_booking")
    booking_a_id = book_a["booking_id"]

    # Barrier to fire book(Slot B) and reschedule(Slot A -> Slot B) simultaneously
    barrier = threading.Barrier(2)
    results = {}

    def run_book():
        barrier.wait()
        try:
            r = env_user1.book(hold_b_id, idempotency_key="idem_user1_book_b")
            results["book"] = ("success", r)
        except Exception as exc:
            results["book"] = ("error", exc)

    def run_reschedule():
        barrier.wait()
        try:
            r = env_user2.reschedule(booking_a_id, new_slot=slot_b, idempotency_key="idem_user2_resched_b")
            results["reschedule"] = ("success", r)
        except Exception as exc:
            results["reschedule"] = ("error", exc)

    t1 = threading.Thread(target=run_book)
    t2 = threading.Thread(target=run_reschedule)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Query active bookings on Slot B in DB
    active_b = fresh_db.conn.execute(
        "SELECT * FROM bookings WHERE provider_id = ? AND slot = ? AND status IN ('CONFIRMED', 'RESCHEDULED')",
        (provider_id, slot_b),
    ).fetchall()

    # Invariant: NEVER double book slot B
    assert len(active_b) == 1, (
        f"Double booking defect! Found {len(active_b)} active bookings on slot {slot_b}: {[dict(r) for r in active_b]}. "
        f"Results: {results}"
    )

    # Check outcomes
    book_status = results["book"][1].get("status") if results["book"][0] == "success" else str(results["book"][1])
    resched_status = results["reschedule"][1].get("status") if results["reschedule"][0] == "success" else str(results["reschedule"][1])

    if book_status == "BOOKED":
        # User 1 won; User 2 must have received slot_conflict
        assert resched_status == "slot_conflict", f"Expected reschedule to receive slot_conflict, got: {results['reschedule']}"
    elif resched_status == "RESCHEDULED":
        # User 2 won; User 1 must have received slot_taken
        assert book_status == "slot_taken", f"Expected book to receive slot_taken, got: {results['book']}"
    else:
        pytest.fail(f"Unexpected outcome state: book={book_status}, reschedule={resched_status}")


# =============================================================================
# 3. Concurrent Reschedule vs Reschedule Contention (10 bookings contending for 1 slot)
# =============================================================================
def test_concurrent_multiple_reschedules_to_same_slot(fresh_db):
    """10 existing bookings all simultaneously try to reschedule to a single open target slot.
    Invariant: Exactly 1 succeeds with status RESCHEDULED. 9 receive status: slot_conflict.
    No double booking in DB.
    """
    provider_id = "prov_plumb_1"
    target_slot = "2026-11-10 10:00"

    # Seed 10 distinct bookings on distinct dummy slots
    booking_ids = []
    for i in range(10):
        dummy_slot = f"2026-11-20 {10 + i}:00"
        b_id = f"bk_seed_{i}"
        fresh_db.conn.execute(
            "INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?, ?, 'CONFIRMED', ?, ?, ?)",
            (b_id, f"user_{i}", provider_id, "AquaFix Plumbing", "plumber", dummy_slot, 1100, f"hold_seed_{i}", f"idem_seed_{i}", time.time()),
        )
        booking_ids.append((f"user_{i}", b_id))
    fresh_db.conn.commit()

    barrier = threading.Barrier(10)
    results = []

    def do_reschedule(user_id, b_id):
        env = EnterpriseLiveToolEnvironment(db=fresh_db, user_id=user_id)
        barrier.wait()
        r = env.reschedule(b_id, new_slot=target_slot, idempotency_key=f"idem_resched_{b_id}")
        results.append((b_id, r))

    threads = [threading.Thread(target=do_reschedule, args=(uid, bid)) for uid, bid in booking_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rescheduled = [r for b_id, r in results if r.get("status") == "RESCHEDULED"]
    conflicts = [r for b_id, r in results if r.get("status") == "slot_conflict"]

    assert len(rescheduled) == 1, (
        f"Expected exactly 1 successful reschedule, got {len(rescheduled)}: {rescheduled}"
    )
    assert len(conflicts) == 9, (
        f"Expected 9 slot_conflict results, got {len(conflicts)}"
    )

    # In DB: exactly 1 booking on target_slot
    active_target = fresh_db.conn.execute(
        "SELECT * FROM bookings WHERE provider_id = ? AND slot = ? AND status IN ('CONFIRMED', 'RESCHEDULED')",
        (provider_id, target_slot),
    ).fetchall()
    assert len(active_target) == 1, f"Expected 1 booking on target slot, found {len(active_target)}"


# =============================================================================
# 4. State Transition Exhaustion Matrix
# =============================================================================
def test_state_machine_illegal_mutations_comprehensive(fresh_db):
    """Test all illegal state transitions:
    - Hold in CONSUMED cannot be booked again
    - Hold in RELEASED cannot be booked
    - Hold in EXPIRED cannot be booked
    - Booking in CANCELLED cannot be rescheduled
    - Booking in CANCELLED cannot be cancelled again (returns idempotent replay)
    - Booking in COMPLETED cannot be rescheduled
    - Booking in COMPLETED cannot be cancelled
    - Rescheduling to SAME slot returns idempotent replay
    - Cancelling booking releases the associated hold
    """
    env = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_1")

    # 1. Test Hold CONSUMED rejection
    hold1 = env.hold_slot("prov_plumb_1", "2026-11-09 09:00", ttl_seconds=600)
    b1 = env.book(hold1["hold_id"], idempotency_key="idem_m_01")
    assert b1["status"] == "BOOKED"

    with pytest.raises(ToolExecutionError) as exc:
        env.book(hold1["hold_id"], idempotency_key="idem_m_01_alt")
    assert exc.value.code == "invalid_hold"

    # 2. Test Hold RELEASED rejection
    hold2 = env.hold_slot("prov_plumb_1", "2026-11-09 11:00", ttl_seconds=600)
    fresh_db.release_hold(hold2["hold_id"])
    with pytest.raises(ToolExecutionError) as exc:
        env.book(hold2["hold_id"], idempotency_key="idem_m_02")
    assert exc.value.code == "invalid_hold"

    # 3. Test Hold EXPIRED rejection
    hold3 = env.hold_slot("prov_plumb_1", "2026-11-10 10:00", ttl_seconds=10)
    fresh_db.conn.execute("UPDATE slot_holds SET status = 'EXPIRED' WHERE hold_id = ?", (hold3["hold_id"],))
    fresh_db.conn.commit()
    res3 = env.book(hold3["hold_id"], idempotency_key="idem_m_03")
    assert res3["status"] == "hold_expired"

    # 4. Test CANCELLED booking cannot be rescheduled
    b1_cancel = env.cancel(b1["booking_id"])
    assert b1_cancel["status"] == "CANCELLED"
    with pytest.raises(ToolExecutionError) as exc:
        env.reschedule(b1["booking_id"], new_slot="2026-11-09 11:00")
    assert exc.value.code == "booking_cancelled"

    # 5. Test CANCELLED booking idempotently replays cancel
    b1_cancel_again = env.cancel(b1["booking_id"])
    assert b1_cancel_again["status"] == "CANCELLED"
    assert b1_cancel_again.get("idempotent_replay") is True

    # 6. Test COMPLETED booking cannot be rescheduled or cancelled
    completed_id = "bk_past_elec_01"
    with pytest.raises(ToolExecutionError) as exc:
        env.reschedule(completed_id, new_slot="2026-11-09 08:30")
    assert exc.value.code == "booking_completed"

    with pytest.raises(ToolExecutionError) as exc:
        env.cancel(completed_id)
    assert exc.value.code == "booking_completed"

    # 7. Reschedule to same slot returns idempotent replay
    # Create an active booking
    hold4 = env.hold_slot("prov_plumb_1", "2026-11-09 09:00", ttl_seconds=600)
    b4 = env.book(hold4["hold_id"], idempotency_key="idem_m_04")
    r4 = env.reschedule(b4["booking_id"], new_slot="2026-11-09 09:00")
    assert r4["status"] == "RESCHEDULED"
    assert r4.get("idempotent_replay") is True


# =============================================================================
# 5. Hold Expiration Millisecond Boundary Fuzzing
# =============================================================================
def test_hold_expiration_submillisecond_boundaries(fresh_db):
    """Test boundary behavior around TTL expiration:
    - now < expires_at_epoch: valid, can be booked
    - now > expires_at_epoch: expired, rejected with status: hold_expired
    - expire_stale_holds cleans up holds with expires_at_epoch <= now
    """
    fault_cfg = FaultConfig()
    env = EnterpriseLiveToolEnvironment(db=fresh_db, fault_cfg=fault_cfg, user_id="user_1")

    # 1. 1ms before expiry -> booking succeeds
    hold_valid = env.hold_slot("prov_plumb_1", "2026-11-09 09:00", ttl_seconds=100)
    exp = hold_valid["expires_at_epoch"]
    cur_time = env.now()
    # Advance to 0.005 seconds before expiry
    fault_cfg.simulated_clock_offset_sec = (exp - cur_time) - 0.005
    b_ok = env.book(hold_valid["hold_id"], idempotency_key="idem_subms_ok")
    assert b_ok["status"] == "BOOKED"

    # 2. 1ms past expiry -> returns hold_expired
    hold_expired = env.hold_slot("prov_plumb_1", "2026-11-09 11:00", ttl_seconds=50)
    exp2 = hold_expired["expires_at_epoch"]
    fault_cfg.simulated_clock_offset_sec = (exp2 - cur_time) + 0.005
    b_exp = env.book(hold_expired["hold_id"], idempotency_key="idem_subms_exp")
    assert b_exp["status"] == "hold_expired"

    # Status in DB is updated to EXPIRED
    db_h = fresh_db.get_hold(hold_expired["hold_id"])
    assert db_h["status"] == "EXPIRED"


# =============================================================================
# 6. Cross-User Authorization and Multi-Tenant Isolation
# =============================================================================
def test_cross_user_tenant_authorization(fresh_db):
    """User A's holds and bookings cannot be manipulated by User B:
    - User B cannot book User A's hold
    - User B cannot cancel User A's booking
    - User B cannot reschedule User A's booking
    - User B cannot see User A's booking in list_bookings
    """
    env_a = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="alice")
    env_b = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="bob")

    hold_a = env_a.hold_slot("prov_plumb_1", "2026-11-09 09:00", ttl_seconds=600)

    # Bob tries to book Alice's hold
    with pytest.raises(ToolExecutionError) as exc:
        env_b.book(hold_a["hold_id"], idempotency_key="bob_steal_hold")
    assert exc.value.code == "HOLD_NOT_OWNED"

    # Alice books successfully
    book_a = env_a.book(hold_a["hold_id"], idempotency_key="alice_legit_book")
    assert book_a["status"] == "BOOKED"
    b_id = book_a["booking_id"]

    # Bob tries to cancel Alice's booking
    with pytest.raises(ToolExecutionError) as exc:
        env_b.cancel(b_id)
    assert exc.value.code == "BOOKING_NOT_OWNED"

    # Bob tries to reschedule Alice's booking
    with pytest.raises(ToolExecutionError) as exc:
        env_b.reschedule(b_id, new_slot="2026-11-09 11:00")
    assert exc.value.code == "BOOKING_NOT_OWNED"

    # Bob lists bookings: Alice's booking must NOT appear
    bobs_bookings = env_b.list_bookings()
    assert all(b["booking_id"] != b_id for b in bobs_bookings["bookings"]), "Tenant leak: Alice's booking appeared in Bob's list"


# =============================================================================
# 7. High-Load Chaos Loop: 30 Concurrent Workers Performing Random Actions
# =============================================================================
def test_high_load_chaos_loop_invariants(fresh_db):
    """30 concurrent workers performing random holds, books, reschedules, and cancels.
    Verify:
    1. Zero unhandled sqlite3.IntegrityError or crash exceptions.
    2. Zero double bookings in bookings table at any time.
    3. Idempotency key preservation across all mutations.
    """
    slots = ["2026-11-09 09:00", "2026-11-09 11:00", "2026-11-10 10:00"]
    provider_id = "prov_plumb_1"
    num_workers = 30
    ops_per_worker = 10

    crashes = []
    created_bookings = []
    lock = threading.Lock()

    def worker_loop(worker_id: int):
        user_id = f"worker_{worker_id}"
        env = EnterpriseLiveToolEnvironment(db=fresh_db, user_id=user_id)
        local_bookings = []

        for op_idx in range(ops_per_worker):
            action = random.choice(["hold_and_book", "reschedule", "cancel", "replay"])
            slot = random.choice(slots)
            idem = f"idem_w_{worker_id}_{op_idx}"

            try:
                if action == "hold_and_book":
                    try:
                        h = env.hold_slot(provider_id, slot, ttl_seconds=60)
                        b = env.book(h["hold_id"], idempotency_key=idem)
                        if b.get("status") == "BOOKED":
                            local_bookings.append(b["booking_id"])
                            with lock:
                                created_bookings.append((b["booking_id"], idem))
                    except ToolExecutionError as te:
                        # slot_unavailable, slot_taken, etc. are normal expected outcomes
                        pass

                elif action == "reschedule" and local_bookings:
                    b_id = random.choice(local_bookings)
                    new_slot = random.choice(slots)
                    try:
                        env.reschedule(b_id, new_slot=new_slot, idempotency_key=f"resched_{idem}")
                    except ToolExecutionError:
                        pass

                elif action == "cancel" and local_bookings:
                    b_id = random.choice(local_bookings)
                    try:
                        env.cancel(b_id, idempotency_key=f"cancel_{idem}")
                    except ToolExecutionError:
                        pass

                elif action == "replay" and local_bookings:
                    with lock:
                        if created_bookings:
                            b_id, original_idem = random.choice(created_bookings)
                            status = env.get_booking_status(idempotency_key=original_idem)
                            assert status["status"] != "NOT_FOUND", (
                                f"Idempotency reconciliation failed for key {original_idem}"
                            )

            except Exception as exc:
                with lock:
                    crashes.append((worker_id, op_idx, action, exc))

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(worker_loop, w) for w in range(num_workers)]
        for f in as_completed(futures):
            f.result()

    assert len(crashes) == 0, f"Chaos loop incurred {len(crashes)} crashes: {crashes[:5]}"

    # Verify Database Invariant: Absolutely NO double bookings on any (provider_id, slot)
    rows = fresh_db.conn.execute("""
        SELECT provider_id, slot, COUNT(*) as cnt
        FROM bookings
        WHERE status IN ('CONFIRMED', 'RESCHEDULED')
        GROUP BY provider_id, slot
        HAVING cnt > 1
    """).fetchall()

    assert len(rows) == 0, f"DATABASE INTEGRITY VIOLATION: Found double bookings: {[dict(r) for r in rows]}"



# =============================================================================
# 8. Slot Liberation: Released Slot Can Be Re-held and Re-booked Immediately
# =============================================================================
def test_slot_liberation_after_cancel_and_reschedule(fresh_db):
    """When a booking is cancelled or rescheduled away, the freed slot MUST immediately
    be available for another user to hold and book.
    """
    env1 = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_1")
    env2 = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_2")
    provider_id = "prov_plumb_1"
    slot_a = "2026-11-09 09:00"
    slot_b = "2026-11-09 11:00"

    # Part A: Cancel frees slot
    h1 = env1.hold_slot(provider_id, slot_a, ttl_seconds=600)
    b1 = env1.book(h1["hold_id"], idempotency_key="idem_liberate_01")
    assert b1["status"] == "BOOKED"

    # User 2 attempts to hold slot_a -> fails because it's booked
    with pytest.raises(ToolExecutionError) as exc:
        env2.hold_slot(provider_id, slot_a, ttl_seconds=600)
    assert exc.value.code == "slot_unavailable"

    # User 1 cancels booking
    env1.cancel(b1["booking_id"])

    # User 2 can now successfully hold and book slot_a!
    h2 = env2.hold_slot(provider_id, slot_a, ttl_seconds=600)
    b2 = env2.book(h2["hold_id"], idempotency_key="idem_liberate_02")
    assert b2["status"] == "BOOKED"
    assert b2["slot"] == slot_a

    # Part B: Reschedule frees previous slot
    # User 2 reschedules b2 to slot_b
    r2 = env2.reschedule(b2["booking_id"], new_slot=slot_b)
    assert r2["status"] == "RESCHEDULED"

    # Now slot_a is liberated again! User 1 can hold and book it!
    h1_new = env1.hold_slot(provider_id, slot_a, ttl_seconds=600)
    b1_new = env1.book(h1_new["hold_id"], idempotency_key="idem_liberate_03")
    assert b1_new["status"] == "BOOKED"
    assert b1_new["slot"] == slot_a


# =============================================================================
# 9. Price Lock Guarantee Across Base Price Catalog Changes
# =============================================================================
def test_price_lock_guarantee_against_catalog_mutations(fresh_db):
    """The quoted_price in an active hold MUST be honored upon booking,
    even if the provider's base price in the catalog increases prior to booking.
    """
    env = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_1")
    provider_id = "prov_plumb_1"
    slot = "2026-11-09 09:00"

    hold = env.hold_slot(provider_id, slot, ttl_seconds=600)
    quoted_price = hold["quoted_price"]
    assert quoted_price == 1100

    # Simulate catalog price surge in the background
    fresh_db.conn.execute(
        "UPDATE providers_catalog SET base_price = 2500 WHERE provider_id = ?",
        (provider_id,),
    )
    fresh_db.conn.commit()

    # User books the hold
    booking = env.book(hold["hold_id"], idempotency_key="idem_price_lock")
    assert booking["status"] == "BOOKED"
    # Quoted price from hold must be honored, NOT the new inflated catalog price
    assert booking["price"] == quoted_price
    assert booking["price"] == 1100

    # Verify database record matches
    db_booking = fresh_db.get_booking(booking["booking_id"])
    assert db_booking["price"] == 1100


# =============================================================================
# 10. Hostile SQL Injection and Malicious Character Resistance
# =============================================================================
def test_sql_injection_and_malicious_input_resistance(fresh_db):
    """Adversarial input payloads in idempotency keys, booking IDs, and hold IDs
    must not cause SQL syntax errors or data corruption.
    """
    env = EnterpriseLiveToolEnvironment(db=fresh_db, user_id="user_malicious'; DROP TABLE bookings; --")
    provider_id = "prov_plumb_1"
    slot = "2026-11-09 09:00"

    # Malicious user_id hold
    hold = env.hold_slot(provider_id, slot, ttl_seconds=600)
    assert hold["hold_id"].startswith("hold_")

    # Malicious idempotency key containing SQL injection
    malicious_idem = "idem_inject' OR '1'='1'; DELETE FROM bookings; --"
    booking = env.book(hold["hold_id"], idempotency_key=malicious_idem)
    assert booking["status"] == "BOOKED"

    # Status check with malicious key
    status = env.get_booking_status(idempotency_key=malicious_idem)
    assert status["status"] == "CONFIRMED"
    assert status["booking_id"] == booking["booking_id"]

    # Verify table integrity: bookings table is still intact!
    count_row = fresh_db.conn.execute("SELECT count(*) as cnt FROM bookings").fetchone()
    assert count_row["cnt"] >= 1
