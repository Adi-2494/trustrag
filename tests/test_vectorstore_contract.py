"""
P7 `VectorStore` contract (docs/phases/p7-plan.md 3.1, 3.2; ADR 0013; SPEC-GAP-066, -067, -076).

Every test runs against the real `ChromaStore` and, when the server backends are configured, `PgVectorStore`
(fixture `real_store`, tests/conftest.py). The `[PgVectorStore]` cases skip without TRUSTRAG_TEST_* URLs.
"""

import contextlib
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from src import config
from tests import server_support

LEAVE = "Employees receive 25 days of paid annual leave each year."
CANTEEN = "The canteen serves hot lunch from noon until two in the afternoon."
PARKING = "Visitor parking is available behind the north building."


def chunk(cid, text, file_name="handbook.pdf", page=1):
    return {"id": cid, "text": text, "file_name": file_name, "page": page}


def _is_pg(store):
    return type(store).__name__ == "PgVectorStore"


def _age(store, monkeypatch, hours):
    """Make every existing workspace look `hours` old: backdate rows in Postgres, shift the clock for ChromaDB."""
    if _is_pg(store):
        server_support.pg_rows("UPDATE vector_workspaces SET created = created - %s", (hours * 3600,))
    else:
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + hours * 3600)


def test_add_and_retrieve_orders_by_cosine_distance(real_store):
    """P7-AC1 (contract): the chunk about leave is closest to a leave question; distances never decrease."""
    ws = real_store.new_workspace()
    real_store.add_chunks(ws, [chunk("c1", CANTEEN), chunk("c2", LEAVE), chunk("c3", PARKING)])
    hits = real_store.retrieve(ws, "How many vacation days do employees get?", 3)
    assert hits[0]["id"] == "c2"
    distances = [h["distance"] for h in hits]
    assert distances == sorted(distances) and all(0.0 <= d <= 2.0 for d in distances)
    assert {k: hits[0][k] for k in ("id", "text", "file_name", "page")} == chunk("c2", LEAVE)


def test_retrieve_empty_workspace_returns_empty(real_store):
    """P7-AC1 (contract): a workspace with no chunks (or never used) yields [] without an error."""
    assert real_store.retrieve(real_store.new_workspace(), "anything", 4) == []
    assert real_store.count(real_store.new_workspace()) == 0
    assert real_store.list_files(real_store.new_workspace()) == []


def test_retrieve_returns_at_most_count_and_rounds_distance(real_store):
    """P7-AC1 (contract): min(top_k, stored) hits; distance is a float rounded to 3 places."""
    ws = real_store.new_workspace()
    real_store.add_chunks(ws, [chunk("c1", CANTEEN), chunk("c2", LEAVE), chunk("c3", PARKING)])
    assert len(real_store.retrieve(ws, "leave", 10)) == 3
    assert len(real_store.retrieve(ws, "leave", 2)) == 2
    for hit in real_store.retrieve(ws, "leave", 3):
        assert isinstance(hit["distance"], float) and hit["distance"] == round(hit["distance"], 3)


def test_upsert_replaces_same_chunk_id(real_store):
    """P7-AC1 (contract): adding an existing chunk id replaces it; nothing is duplicated."""
    ws = real_store.new_workspace()
    real_store.add_chunks(ws, [chunk("c1", LEAVE)])
    real_store.add_chunks(ws, [chunk("c1", PARKING, page=7)])
    assert real_store.count(ws) == 1
    assert real_store.all_chunks(ws) == [chunk("c1", PARKING, page=7)]


def test_delete_file_only_touches_that_file_in_that_workspace(real_store):
    """P7-AC1 (contract) / threat 48: same file name in another workspace and other files in this one survive."""
    mine, theirs = real_store.new_workspace(), real_store.new_workspace()
    real_store.add_chunks(mine, [chunk("a1", LEAVE), chunk("b1", PARKING, file_name="site.pdf")])
    real_store.add_chunks(theirs, [chunk("a1", CANTEEN)])
    real_store.delete_file(mine, "handbook.pdf")
    assert real_store.list_files(mine) == ["site.pdf"] and real_store.count(mine) == 1
    assert real_store.all_chunks(theirs) == [chunk("a1", CANTEEN)]
    real_store.delete_file(mine, "handbook.pdf")  # deleting again is harmless


def test_list_files_sorted_unique_and_count(real_store):
    """P7-AC1 (contract): file names sorted and unique; count is the number of chunks."""
    ws = real_store.new_workspace()
    real_store.add_chunks(
        ws,
        [chunk("1", LEAVE, "b.pdf"), chunk("2", CANTEEN, "a.pdf"), chunk("3", PARKING, "b.pdf", page=2)],
    )
    assert real_store.list_files(ws) == ["a.pdf", "b.pdf"] and real_store.count(ws) == 3


def test_all_chunks_returns_every_chunk(real_store):
    """P7-AC1 (contract): all_chunks returns each stored chunk exactly (id, text, file_name, page)."""
    ws = real_store.new_workspace()
    stored = [chunk("1", LEAVE, "a.pdf", 1), chunk("2", CANTEEN, "a.pdf", 2), chunk("3", PARKING, "b.pdf", 5)]
    real_store.add_chunks(ws, stored)
    assert sorted(real_store.all_chunks(ws), key=lambda c: c["id"]) == stored


def test_clear_is_idempotent_and_workspace_scoped(real_store):
    """P7-AC1 (contract) / threat 4, 48: clear twice is fine; another workspace is untouched; the id can be reused."""
    mine, theirs = real_store.new_workspace(), real_store.new_workspace()
    real_store.add_chunks(mine, [chunk("c1", LEAVE)])
    real_store.add_chunks(theirs, [chunk("c1", LEAVE)])
    real_store.clear(mine)
    real_store.clear(mine)
    assert real_store.retrieve(mine, "leave", 4) == [] and real_store.count(mine) == 0
    assert real_store.count(theirs) == 1
    real_store.add_chunks(mine, [chunk("c9", PARKING)])
    assert real_store.count(mine) == 1


BAD_IDS = [
    "",
    "../etc/passwd",
    "x" * 32,
    "A" * 32,
    "a" * 31,
    "a" * 33,
    "ws_" + "a" * 32,
    "acct_" + "a" * 32,
    "acct-" + "g" * 32,
    "a" * 32 + "'; DROP TABLE chunks;--",
    "a" * 32 + "\n",
    "acct-",
    "\x00" * 32,
]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_invalid_workspace_ids_raise_value_error_before_any_query(real_store, bad):
    """P7-AC1 (contract) / threat 48: malformed workspace ids are rejected (ValueError) by every operation."""
    with pytest.raises(ValueError):
        real_store.add_chunks(bad, [chunk("c1", LEAVE)])
    for call in (
        lambda: real_store.retrieve(bad, "leave", 4),
        lambda: real_store.all_chunks(bad),
        lambda: real_store.delete_file(bad, "handbook.pdf"),
        lambda: real_store.list_files(bad),
        lambda: real_store.count(bad),
    ):
        with pytest.raises(ValueError):
            call()
    with contextlib.suppress(ValueError):  # `clear` may ignore a bad id (v1.0 behaviour) but must never act on it
        real_store.clear(bad)


def test_cleanup_deletes_old_anonymous_keeps_listed_and_never_ages_out_account(real_store, monkeypatch):
    """P7-AC1 (contract) / threat 33: past 24 h only unlisted anonymous workspaces go; `keep` and `acct-` stay."""
    old, kept, account = (
        real_store.new_workspace(),
        real_store.new_workspace(),
        real_store.new_workspace(persistent=True),
    )
    assert re.fullmatch(r"acct-[0-9a-f]{32}", account)
    for ws in (old, kept, account):
        real_store.add_chunks(ws, [chunk("c1", LEAVE)])
    _age(real_store, monkeypatch, config.WORKSPACE_MAX_AGE_HOURS + 1)
    assert real_store.cleanup(keep=frozenset({kept})) == 1
    assert real_store.count(old) == 0 and real_store.count(kept) == 1 and real_store.count(account) == 1


def test_cleanup_purges_only_tombstoned_account_workspaces(real_store):
    """P7-AC1 (contract) / threat 38: `purge` removes exactly the listed account workspaces, whatever their age."""
    doomed, spared = real_store.new_workspace(persistent=True), real_store.new_workspace(persistent=True)
    fresh_anonymous = real_store.new_workspace()
    for ws in (doomed, spared, fresh_anonymous):
        real_store.add_chunks(ws, [chunk("c1", LEAVE)])
    assert real_store.cleanup(purge=frozenset({doomed})) == 1
    assert real_store.count(doomed) == 0 and real_store.count(spared) == 1 and real_store.count(fresh_anonymous) == 1


def test_new_workspace_id_formats(real_store):
    """P7-AC1 (contract): `<32 hex>` for anonymous, `acct-<32 hex>` for account workspaces; always unique."""
    anonymous = {real_store.new_workspace() for _ in range(5)}
    persistent = {real_store.new_workspace(persistent=True) for _ in range(5)}
    assert len(anonymous) == len(persistent) == 5
    assert all(re.fullmatch(r"[0-9a-f]{32}", w) for w in anonymous)
    assert all(re.fullmatch(r"acct-[0-9a-f]{32}", w) for w in persistent)


# ---------------------------------------------------------------------------- pgvector specifics (server backends only)
def _pg_store(server_backend):
    from src.pgvector_store import PgVectorStore

    return PgVectorStore()


def test_cleanup_removes_chunks_without_workspace_row(server_backend):
    """P7-AC1 (contract) / threat 56: chunks left without a `vector_workspaces` row are removed by cleanup."""
    store = _pg_store(server_backend)
    live = store.new_workspace()
    store.add_chunks(live, [chunk("c1", LEAVE)])
    orphan = uuid.uuid4().hex
    vector = "[" + ",".join(["0.1"] * config.EMBEDDING_DIM) + "]"
    server_support.pg_rows(
        "INSERT INTO chunks (workspace, id, text, file_name, page, embedding) VALUES (%s, 'o1', 'orphan', 'x.pdf', 1, %s::vector)",
        (orphan, vector),
    )
    assert server_support.pg_rows("SELECT count(*) FROM chunks WHERE workspace = %s", (orphan,)) == [(1,)]
    store.cleanup()
    assert server_support.pg_rows("SELECT count(*) FROM chunks WHERE workspace = %s", (orphan,)) == [(0,)]
    assert store.count(live) == 1


class _Overlap:
    """Wraps `pgvector_store._transaction` so ONE add transaction pauses, holding the workspace row lock, until the
    clearing thread has started: the two transactions provably overlap (review: the old race test often did not)."""

    def __init__(self, real):
        self.real = real
        self.row_locked = threading.Event()
        self.clear_started = threading.Event()
        self.overlapped = False

    def __call__(self):
        outer = self
        manager = self.real()

        class Tx:
            def __enter__(self):
                self.conn = manager.__enter__()
                return self

            def __exit__(self, *exc):
                return manager.__exit__(*exc)

            def execute(self, sql, *args, **kwargs):
                result = self.conn.execute(sql, *args, **kwargs)
                if str(sql).lstrip().startswith("INSERT INTO vector_workspaces"):
                    outer.row_locked.set()
                    outer.overlapped = outer.clear_started.wait(timeout=2.0) or outer.overlapped
                return result

            def __getattr__(self, name):
                return getattr(self.conn, name)

        return Tx()


def test_add_chunks_racing_clear_leaves_no_reachable_or_kept_chunks(server_backend, monkeypatch):
    """P7-AC1 (contract) / threat 56: add vs clear that REALLY overlap never leave a chunk without its workspace row."""
    from src import pgvector_store

    store = _pg_store(server_backend)
    monkeypatch.setattr(pgvector_store, "_embed", lambda texts: ["[" + ",".join(["0.1"] * 384) + "]"] * len(texts))
    for _ in range(5):
        ws = store.new_workspace(persistent=True)
        overlap = _Overlap(pgvector_store._transaction)
        with monkeypatch.context() as patch:
            patch.setattr(pgvector_store, "_transaction", overlap)

            def add(ws=ws):
                store.add_chunks(ws, [chunk(f"c{i}", LEAVE) for i in range(5)])

            def clear(ws=ws, overlap=overlap):
                assert overlap.row_locked.wait(timeout=10), "the add transaction never took the workspace row"
                overlap.clear_started.set()
                store.clear(ws)

            with ThreadPoolExecutor(2) as pool:
                for future in [pool.submit(add), pool.submit(clear)]:
                    future.result(timeout=60)
        assert overlap.overlapped, "the transactions did not overlap: the test proves nothing"
        orphans = server_support.pg_rows(
            "SELECT count(*) FROM chunks c WHERE NOT EXISTS (SELECT 1 FROM vector_workspaces w WHERE w.workspace = c.workspace)"
        )
        assert orphans == [(0,)]
        assert store.count(ws) in (0, 5)
        store.clear(ws)  # what the service does after a racing delete
        store.cleanup(purge=frozenset({ws}))
        assert store.count(ws) == 0


def test_text_with_nul_is_refused_not_altered(server_backend):
    """P7-AC1 (contract) / SPEC-GAP-076: U+0000 in chunk text is refused (not a ValueError, so never a 404) and nothing is stored."""
    store = _pg_store(server_backend)
    ws = store.new_workspace()
    with pytest.raises(Exception) as caught:
        store.add_chunks(ws, [chunk("ok", LEAVE), chunk("bad", "before\x00after")])
    assert not isinstance(caught.value, ValueError)
    assert store.count(ws) == 0  # nothing from the failed batch was stored, and no chunk was "cleaned"
    store.add_chunks(ws, [chunk("fine", "no nul here")])
    assert store.all_chunks(ws)[0]["text"] == "no nul here"


def test_semantic_ranking_matches_between_chroma_and_pgvector(server_backend, monkeypatch):
    """P7-AC1 (contract) / P7-AC3: both stores rank the same chunks in the same order for the same questions."""
    from src import vectorstore
    from src.pgvector_store import PgVectorStore

    monkeypatch.setattr(vectorstore, "_client", None)
    monkeypatch.setattr(vectorstore, "_collections", {})
    stores = [vectorstore.ChromaStore(), PgVectorStore()]
    corpus = [
        chunk("c1", LEAVE),
        chunk("c2", CANTEEN),
        chunk("c3", PARKING),
        chunk("c4", "Expense reports must be filed within thirty days of travel."),
        chunk("c5", "Remote work is allowed up to two days per week with manager approval."),
    ]
    questions = [
        "How many vacation days?",
        "Where can visitors park?",
        "When is lunch served?",
        "work from home policy",
    ]
    rankings = []
    for store in stores:
        ws = store.new_workspace()
        store.add_chunks(ws, corpus)
        rankings.append([[h["id"] for h in store.retrieve(ws, q, 5)] for q in questions])
    assert rankings[0] == rankings[1]
