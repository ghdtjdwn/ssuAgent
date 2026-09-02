"""PostgreSQL-specific conversation lifecycle regressions.

Set ``SSUAGENT_TEST_DATABASE_URL`` to a disposable PostgreSQL database to run
the integration cases. The default test still verifies the lock-bearing fence
DDL so an accidental removal is caught without external infrastructure.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import DatabaseError
from psycopg_pool import AsyncConnectionPool

from ssu_agent import main


class _RecordingCursor:
    def __init__(self, queries: list[str]) -> None:
        self.queries = queries

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, query: str, params: tuple | None = None) -> None:
        self.queries.append(" ".join(query.split()))


class _RecordingConnection:
    def __init__(self, queries: list[str]) -> None:
        self.queries = queries

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def transaction(self):
        return self

    def cursor(self):
        return _RecordingCursor(self.queries)


class _RecordingPool:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def connection(self):
        return _RecordingConnection(self.queries)


@pytest.mark.asyncio
async def test_checkpoint_fence_ddl_locks_owner_before_accepting_writes() -> None:
    pool = _RecordingPool()

    await main._setup_thread_owners(pool)

    ddl = "\n".join(pool.queries)
    assert "FROM thread_owners" in ddl
    assert "FOR SHARE" in ddl
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        assert f"BEFORE INSERT OR UPDATE ON {table}" in ddl


def test_scrub_sql_materializes_owner_lock_before_checkpoint_update() -> None:
    queries = [
        main._CHECKPOINT_CAPABILITY_SCRUB_SQL,
        main._TYPED_CAPABILITY_SCRUB_SQL.format(table="checkpoint_writes"),
    ]
    for query in queries:
        normalized = " ".join(query.split())
        assert "locked_owners AS MATERIALIZED" in normalized
        assert "ORDER BY owners.thread_id FOR SHARE OF owners" in normalized
        assert normalized.index("FOR SHARE OF owners") < normalized.index("UPDATE")
        assert "FOR UPDATE SKIP LOCKED" not in normalized


TEST_DATABASE_URL = os.getenv("SSUAGENT_TEST_DATABASE_URL", "")
requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="SSUAGENT_TEST_DATABASE_URL is not configured",
)


async def _delete_fixture_rows(pool: AsyncConnectionPool, thread_id: str) -> None:
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    await cur.execute(f"DELETE FROM {table} WHERE thread_id = %s", (thread_id,))
                await cur.execute("DELETE FROM thread_owners WHERE thread_id = %s", (thread_id,))


async def _wait_for_checkpoint_delete_advisory_wait(pool: AsyncConnectionPool) -> None:
    for _ in range(100):
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND pid <> pg_backend_pid()
                          AND wait_event = 'advisory'
                          AND query LIKE 'DELETE FROM checkpoints%'
                    )
                    """
                )
                if (await cur.fetchone())[0]:
                    return
        await asyncio.sleep(0.01)
    raise AssertionError("lifecycle delete did not reach the advisory-lock test barrier")


@requires_postgres
@pytest.mark.asyncio
async def test_postgres_scrub_replaces_only_legacy_capability_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = f"scrub-{uuid.uuid4()}"
    async with AsyncConnectionPool(
        conninfo=TEST_DATABASE_URL,
        min_size=1,
        max_size=3,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    ) as pool:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        await main._setup_thread_owners(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO thread_owners (thread_id, owner, owner_kind) "
                    "VALUES (%s, NULL, NULL)",
                    (thread_id,),
                )
                # Seed the exact pre-hardening representation in this disposable
                # database. Re-enable the production fence before the scrub.
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    await cur.execute(
                        f"ALTER TABLE {table} DISABLE TRIGGER ssuagent_{table}_lifecycle_fence"
                    )

        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {
            "mcp_session_id": "raw-bearer",
            "library_connected": True,
        }
        saved_config = await saver.aput(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
            checkpoint,
            {},
            {},
        )
        await saver.aput_writes(
            saved_config,
            [("mcp_session_id", "raw-pending-bearer"), ("safe_channel", "preserve-me")],
            "task-1",
        )
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO checkpoint_blobs
                        (thread_id, checkpoint_ns, channel, version, type, blob)
                    VALUES (%s, '', 'mcp_session_id', 'legacy-v1', 'msgpack', %s)
                    """,
                    (thread_id, b"raw-blob"),
                )
                # Reproduce rows left by the pre-fence DELETE race. The scrub
                # must still sanitize them even though the owner is tombstoned.
                await cur.execute(
                    "UPDATE thread_owners SET owner_kind = 'deleted' WHERE thread_id = %s",
                    (thread_id,),
                )
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    await cur.execute(
                        f"ALTER TABLE {table} ENABLE TRIGGER ssuagent_{table}_lifecycle_fence"
                    )

        # A write from an old pod after the fence is installed is normalized at
        # the DB boundary and therefore does not add to the scrub count.
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE thread_owners SET owner_kind = NULL WHERE thread_id = %s",
                    (thread_id,),
                )
        await saver.aput_writes(
            saved_config,
            [("mcp_session_id", "late-raw-bearer")],
            "task-after-fence",
        )
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE thread_owners SET owner_kind = 'deleted' WHERE thread_id = %s",
                    (thread_id,),
                )

        monkeypatch.setattr(main.config, "AGENT_CAPABILITY_SCRUB_BATCH_SIZE", 1)
        monkeypatch.setattr(main.config, "AGENT_CAPABILITY_SCRUB_MAX_ROWS", 10)
        none_type, none_blob = saver.serde.dumps_typed(None)
        assert (
            await main._scrub_legacy_checkpoint_capabilities(
                pool,
                none_type=none_type,
                none_blob=none_blob,
            )
            == 3
        )
        assert (
            await main._scrub_legacy_checkpoint_capabilities(
                pool,
                none_type=none_type,
                none_blob=none_blob,
            )
            == 0
        )

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT checkpoint #> '{channel_values,mcp_session_id}', "
                    "checkpoint #> '{channel_values,library_connected}' "
                    "FROM checkpoints WHERE thread_id = %s",
                    (thread_id,),
                )
                assert await cur.fetchone() == (None, True)
                await cur.execute(
                    "SELECT channel, type, blob FROM checkpoint_writes "
                    "WHERE thread_id = %s ORDER BY channel",
                    (thread_id,),
                )
                writes = await cur.fetchall()
                assert ("mcp_session_id", none_type, none_blob) in writes
                assert any(row[0] == "safe_channel" and row[2] != none_blob for row in writes)
                await cur.execute(
                    "SELECT type, blob FROM checkpoint_writes "
                    "WHERE thread_id = %s AND task_id = 'task-after-fence'",
                    (thread_id,),
                )
                assert await cur.fetchone() == (none_type, none_blob)

        await _delete_fixture_rows(pool, thread_id)


@requires_postgres
@pytest.mark.parametrize("lifecycle_path", ["delete", "retention"])
@pytest.mark.asyncio
async def test_scrub_and_lifecycle_delete_share_owner_first_lock_order(
    lifecycle_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce the old owner/checkpoint lock inversion with real PostgreSQL.

    A statement trigger pauses lifecycle deletion after it owns the owner row
    but before it touches checkpoint rows. The scrub must then wait on that
    owner without locking the checkpoint candidate. Releasing the barrier lets
    both operations finish; the old checkpoint-first scrub deadlocks here.
    """
    thread_id = f"lock-order-{lifecycle_path}-{uuid.uuid4()}"
    advisory_key = 6_200_000 + (1 if lifecycle_path == "delete" else 2)
    trigger_name = "ssuagent_test_block_checkpoint_delete"
    async with AsyncConnectionPool(
        conninfo=TEST_DATABASE_URL,
        min_size=2,
        max_size=6,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    ) as pool:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        await main._setup_thread_owners(pool)
        monkeypatch.setattr(main, "_pool", pool)
        monkeypatch.setattr(main.config, "AGENT_CAPABILITY_SCRUB_BATCH_SIZE", 10)
        monkeypatch.setattr(main.config, "AGENT_CAPABILITY_SCRUB_MAX_ROWS", 100)
        monkeypatch.setattr(main.config, "AGENT_CONVERSATION_RETENTION_DAYS", 30)
        monkeypatch.setattr(main.config, "AGENT_RETENTION_CLEANUP_BATCH_SIZE", 10)

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO thread_owners "
                    "(thread_id, owner, owner_kind, last_accessed_at) "
                    "VALUES (%s, NULL, NULL, now() - interval '60 days')",
                    (thread_id,),
                )
                await cur.execute(
                    "ALTER TABLE checkpoints DISABLE TRIGGER ssuagent_checkpoints_lifecycle_fence"
                )

        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"mcp_session_id": "legacy-raw-bearer"}
        await saver.aput(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
            checkpoint,
            {},
            {},
        )

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "ALTER TABLE checkpoints ENABLE TRIGGER ssuagent_checkpoints_lifecycle_fence"
                )
                await cur.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON checkpoints")
                await cur.execute(
                    """
                    CREATE OR REPLACE FUNCTION ssuagent_test_wait_before_checkpoint_delete()
                    RETURNS trigger
                    LANGUAGE plpgsql
                    AS $test$
                    BEGIN
                        PERFORM pg_advisory_xact_lock(TG_ARGV[0]::bigint);
                        RETURN NULL;
                    END;
                    $test$
                    """
                )
                await cur.execute(
                    f"""
                    CREATE TRIGGER {trigger_name}
                    BEFORE DELETE ON checkpoints
                    FOR EACH STATEMENT
                    EXECUTE FUNCTION ssuagent_test_wait_before_checkpoint_delete('{advisory_key}')
                    """
                )

        none_type, none_blob = saver.serde.dumps_typed(None)
        async with pool.connection() as blocker_conn:
            async with blocker_conn.cursor() as blocker_cur:
                await blocker_cur.execute("SELECT pg_advisory_lock(%s)", (advisory_key,))
                if lifecycle_path == "delete":
                    lifecycle_task = asyncio.create_task(
                        main.delete_owned_thread(thread_id, None, None)
                    )
                else:
                    lifecycle_task = asyncio.create_task(main.cleanup_expired_threads())

                await _wait_for_checkpoint_delete_advisory_wait(pool)
                scrub_task = asyncio.create_task(
                    main._scrub_legacy_checkpoint_capabilities(
                        pool,
                        none_type=none_type,
                        none_blob=none_blob,
                    )
                )
                await asyncio.sleep(0.05)

                # The paused lifecycle path has not touched checkpoints yet.
                # This NOWAIT lock succeeds only if scrub is also still waiting
                # on the owner row instead of holding the checkpoint row.
                async with pool.connection() as probe_conn:
                    async with probe_conn.transaction():
                        async with probe_conn.cursor() as probe_cur:
                            await probe_cur.execute(
                                "SELECT checkpoint_id FROM checkpoints "
                                "WHERE thread_id = %s FOR UPDATE NOWAIT",
                                (thread_id,),
                            )
                            assert await probe_cur.fetchone() is not None

                await blocker_cur.execute("SELECT pg_advisory_unlock(%s)", (advisory_key,))
                lifecycle_result, scrubbed = await asyncio.wait_for(
                    asyncio.gather(lifecycle_task, scrub_task),
                    timeout=3,
                )

        assert lifecycle_result is True if lifecycle_path == "delete" else lifecycle_result == 1
        assert scrubbed == 0
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON checkpoints")
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    await cur.execute(
                        f"SELECT count(*) FROM {table} WHERE thread_id = %s",
                        (thread_id,),
                    )
                    assert await cur.fetchone() == (0,)

        await _delete_fixture_rows(pool, thread_id)


@requires_postgres
@pytest.mark.asyncio
async def test_delete_commit_fences_late_real_checkpointer_write() -> None:
    thread_id = f"delete-fence-{uuid.uuid4()}"
    async with AsyncConnectionPool(
        conninfo=TEST_DATABASE_URL,
        min_size=2,
        max_size=4,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    ) as pool:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        await main._setup_thread_owners(pool)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO thread_owners (thread_id, owner, owner_kind) "
                    "VALUES (%s, NULL, NULL)",
                    (thread_id,),
                )

        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"library_connected": True}
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        await saver.aput(config, checkpoint, {}, {})

        async with pool.connection() as delete_conn:
            async with delete_conn.transaction():
                async with delete_conn.cursor() as cur:
                    await cur.execute(
                        "SELECT owner_kind FROM thread_owners WHERE thread_id = %s FOR UPDATE",
                        (thread_id,),
                    )
                    await cur.execute("DELETE FROM checkpoints WHERE thread_id = %s", (thread_id,))
                    await cur.execute(
                        "UPDATE thread_owners SET owner = NULL, owner_kind = 'deleted' "
                        "WHERE thread_id = %s",
                        (thread_id,),
                    )

                late_checkpoint = empty_checkpoint()
                late_checkpoint["channel_values"] = {"library_connected": False}
                late_write = asyncio.create_task(saver.aput(config, late_checkpoint, {}, {}))
                await asyncio.sleep(0.05)
                assert not late_write.done(), "late write must wait on the owner lifecycle lock"

            with pytest.raises(DatabaseError):
                await late_write

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    await cur.execute(
                        f"SELECT count(*) FROM {table} WHERE thread_id = %s", (thread_id,)
                    )
                    assert await cur.fetchone() == (0,)

        await _delete_fixture_rows(pool, thread_id)
