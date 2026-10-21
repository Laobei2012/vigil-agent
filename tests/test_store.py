import asyncio

from vigil_agent.state.store import Store


async def claim(store: Store, key="svc|Alert", incident="INC", **kw):
    return await store.claim(
        grouping_key=key, service="svc", alert_name="Alert", incident_id=incident, source="pagerduty", **kw
    )


async def test_concurrent_claims_create_one_investigation(store):
    results = await asyncio.gather(*[claim(store, incident=f"INC{i}") for i in range(20)])
    assert sum(r.created for r in results) == 1
    assert len({r.investigation_id for r in results}) == 1
    rows = await store.fetchall("SELECT * FROM incidents")
    assert len(rows) == 20


async def test_grouping_window(store):
    first = await claim(store, incident="A")
    await store.execute("UPDATE investigations SET window_start = now() - interval '4 minutes'")
    second = await claim(store, incident="B")
    assert second.investigation_id == first.investigation_id
    await store.execute("UPDATE investigations SET window_start = now() - interval '6 minutes'")
    third = await claim(store, incident="C")
    assert third.created and third.investigation_id != first.investigation_id


async def test_redelivered_incident_does_not_claim_again(store):
    first = await claim(store, incident="A")
    again = await claim(store, incident="A")
    assert again.investigation_id == first.investigation_id and not again.created


async def test_expired_lease_is_taken_by_another_owner(store):
    await claim(store)
    assert await store.schedule("pod-a", 3, 5, 90, True) == [1]
    assert await store.schedule("pod-b", 3, 5, 90, True) == []
    await store.execute("UPDATE investigations SET lease_expires_at = now() - interval '1 second'")
    assert await store.schedule("pod-b", 3, 5, 90, True) == [1]
    assert not await store.renew_lease(1, "pod-a", 90)
    assert await store.renew_lease(1, "pod-b", 90)


async def test_live_lease_is_never_taken(store):
    await claim(store)
    await store.schedule("pod-a", 3, 5, 90, True)
    for _ in range(3):
        assert await store.schedule("pod-b", 3, 5, 90, True) == []


async def test_cluster_limit_holds_under_concurrent_schedulers(store):
    for i in range(12):
        await claim(store, key=f"svc|A{i}", incident=f"I{i}")
    taken = await asyncio.gather(*[store.schedule(f"pod-{i}", 3, 5, 90, True) for i in range(6)])
    flat = [x for t in taken for x in t]
    assert len(flat) == 5 and len(set(flat)) == 5
    assert await store.running_count() == 5


async def test_pod_slot_limit(store):
    for i in range(5):
        await claim(store, key=f"svc|A{i}", incident=f"I{i}")
    assert len(await store.schedule("pod-a", 3, 5, 90, True)) == 3


async def test_real_incidents_go_before_early_start(store):
    await claim(store, key="svc|Early", incident=None, early_start=True)
    await claim(store, key="svc|Real", incident="R")
    assert await store.schedule("pod", 1, 5, 90, True) == [2]
    assert await store.schedule("pod", 1, 5, 90, False) == []
    assert await store.schedule("pod", 1, 5, 90, True) == [1]


async def test_early_start_and_page_share_one_investigation(store):
    results = await asyncio.gather(*[claim(store, incident=None, early_start=True) for _ in range(2)])
    assert sum(r.created for r in results) == 1
    await store.execute(
        "UPDATE investigations SET created_at = now() - interval '10 minutes', "
        "window_start = now() - interval '10 minutes'"
    )
    paged = await claim(store, incident="PD1")
    assert paged.investigation_id == results[0].investigation_id and paged.was_held
    inv = await store.get_investigation(paged.investigation_id)
    assert inv["held"] is False


async def test_held_run_is_discarded_after_hold_time(store):
    await claim(store, incident=None, early_start=True)
    await store.execute("UPDATE investigations SET created_at = now() - interval '16 minutes'")
    out = await store.housekeeping(300, 900, 90)
    assert out["discarded"] == 1
    fresh = await claim(store, incident="PD2")
    assert fresh.created


async def test_inbox_is_fifo_per_incident_and_retries(store):
    await store.inbox_put("e1", "INC1", "pagerduty", {"n": 1})
    await store.inbox_put("e2", "INC1", "pagerduty", {"n": 2})
    await store.inbox_put("e3", "INC2", "pagerduty", {"n": 3})
    assert not await store.inbox_put("e1", "INC1", "pagerduty", {"n": 1})

    async with store.inbox_claim(3) as row:
        assert row["event_id"] == "e1"
        async with store.inbox_claim(3) as other:
            assert other["event_id"] == "e3"  # e2 waits behind the locked e1
    async with store.inbox_claim(3) as row:
        assert row["event_id"] == "e2"
        raise RuntimeError("fail on purpose")
    got = await store.fetchone("SELECT status, attempts FROM inbox WHERE event_id = 'e2'")
    assert got == {"status": "pending", "attempts": 1}
    async with store.inbox_claim(3) as row:
        assert row is None  # e2 backs off


async def test_inbox_failure_backs_off_and_dead_letters(store):
    await store.inbox_put("e1", "INC1", "pagerduty", {})
    for _ in range(2):
        async with store.inbox_claim(2) as row:
            raise RuntimeError("pd api down")
        await store.execute("UPDATE inbox SET next_attempt_at = now()")
    got = await store.fetchone("SELECT status, attempts FROM inbox")
    assert got == {"status": "dead", "attempts": 2}
    assert row["_dead"] is True


async def test_spend_and_flaps(store):
    await store.add_spend("investigation", 1.5)
    assert await store.add_spend("early_start", 0.5) == 2.0
    assert [await store.incr_flap("k") for _ in range(3)] == [1, 2, 3]
