# 0012 - Workspace ownership in SQLite, distinct account collections, retention by owner, anonymous workspaces unchanged

**Status:** Proposed (P6). Revised on 2026-11-02 after the plan's security review (`docs/phases/p6-plan.md`
section 9). It amends the scope of ADR 0005: the state database now also holds durable data. It extends the
`VectorStore` interface from ADR 0003 with two backward-compatible keyword parameters.

## Context
- B5: workspaces are isolated by 128-bit ids, and anonymous workspaces expire after 24 h.
  `src/vectorstore.py::cleanup_old_workspaces` deletes every collection whose name starts with `ws_` and whose
  `created` metadata is older than 24 h. `_collection_name` accepts only `^[a-f0-9]{32}$` and returns `ws_<id>`.
- P6-AC1: a user can only list, query or delete their own workspaces. P6-AC5 / SPEC-GAP-004 (decided by the human):
  anonymous workspaces are kept for 24 h; account workspaces are never deleted automatically, only by their owner.
- Security review finding 2: if ownership mattered only while `ACCOUNTS_ENABLED=true`, then turning the flag off, or
  rolling back to v1.0, would make owned workspaces older than 24 h **deleted** by v1.0 cleanup. Their ids would also
  become reachable as anonymous capabilities.
- Security review finding 8: if deleting an owned workspace removed its row, the id would become "unowned", and so
  anonymously reachable.
- ChromaDB collection names must be 3–63 characters, start and end with an alphanumeric character, and otherwise use
  `[a-zA-Z0-9._-]` (`chromadb/api/segment.py::check_index_name`).

## Decision
- **A distinct id format and collection prefix for account workspaces** (SPEC-GAP-053):
  - Account workspace ids are `acct-<32 hex>`, stored in collection `acct_<32 hex>` (37 characters). Anonymous ids
    stay `<32 hex>` → `ws_<32 hex>`.
  - `VectorStore.new_workspace(self, *, persistent: bool = False) -> str` returns the account form when
    `persistent=True`. `ChromaStore._collection_name` accepts both forms and nothing else.
  - What v1.0 code does with these:
    - v1.0 cleanup only touches `ws_`, so it **never deletes** account data.
    - v1.0 `_collection_name` rejects `acct-…` with a `ValueError`, which gives a 404, so v1.0 **never exposes** it.
- **Ownership table**, which is additive:
  `workspaces(workspace TEXT PRIMARY KEY, account_id TEXT NOT NULL, created REAL NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)`,
  with an index on `account_id`. Rows exist only for account workspaces.
- **Authorisation is always on**, whatever the value of `ACCOUNTS_ENABLED`. It lives in one function,
  `TrustRAGService._authorize(workspace, principal)`, which is the **first** statement of every workspace method:
  `list_files`, `count`, `clear`, `upload` (before the size check, the loader, OCR and the scanner) and `ask` (before
  retrieval). The rules are:
  - `acct-` id + a live row owned by the principal → allowed.
  - `acct-` id + no row, a tombstoned row, another owner, or no principal → `WorkspaceNotFound`. This fails closed:
    an `acct-` id without a live row is never treated as anonymous.
  - Hex id + no principal → allowed, which is the v1.0 path. As a defensive check, if a row exists for a hex id, it
    is handled like an `acct-` id.
  - Hex id + a principal → `WorkspaceNotFound` (SPEC-GAP-040).

  `WorkspaceNotFound` subclasses `ValueError`, so it maps to the existing `404 {"detail":"Workspace not found."}`. With
  the flag off, no session can be presented, so owned workspaces are unreachable but preserved.
- **Create:** `new_workspace(*, session)` takes a principal. In one `BEGIN IMMEDIATE` transaction it inserts the row,
  with the cap counted on live rows (`MAX_WORKSPACES_PER_ACCOUNT`, default 10, SPEC-GAP-041).
- **List:** `list_workspaces(*, session)` returns the caller's live rows as `WorkspaceInfo`.
- **Owner delete** (SPEC-GAP-046):
  1. Mark the row `deleted=1`, which is a tombstone. The id is never reusable and never becomes anonymous.
  2. Then call `store.clear(ws)`.
  3. `upload` re-checks authorisation **after** `add_chunks`. If the workspace was tombstoned in the meantime, it
     clears the collection and raises `WorkspaceNotFound`.
  4. `cleanup` also deletes the vector data of tombstoned ids, so a racing write leaves nothing reachable or kept
     (SPEC-GAP-056).
- **Retention:** `VectorStore.cleanup(self, *, keep: frozenset[str] = frozenset(), purge: frozenset[str] = frozenset()) -> int`.
  - `ChromaStore` deletes `ws_*` collections older than 24 h (unchanged), never touches `acct_*` collections unless
    their id is in `purge` (tombstoned), and also skips anything in `keep`.
  - The service passes `keep` = live owned ids and `purge` = tombstoned ids. With no arguments, the behaviour is v1.0.
- **`acct_` collection with no row** (for example, after the state DB was lost): access is refused and the data is
  not deleted automatically. Recovering or deleting it is a human action.
- **Service API shape:** existing `TrustRAGService` methods gain a keyword-only `session: str | None = None`. `None` is
  the v1.0 path.
- **Anonymous behaviour, byte for byte (SPEC-GAP-042):** for the same anonymous requests, status, body and headers
  (except `X-Request-ID`) equal a golden file recorded from pre-P6 code, with the flag on and off. With the flag off,
  the new paths answer as v1.0 did.
- **Files** (SPEC-GAP-044): `CHROMA_DIR` is created or tightened to 0700. The state DB and its `-wal` and `-shm`
  files are created as 0600, and existing files are tightened when the database is opened (upgrade).

## Consequences
- **Rollback to v1.0, or turning the flag off,** neither deletes nor exposes account workspaces. Their documents stay
  invisible until P6 with accounts is back. CHANGELOG and README carry a rollback warning (SPEC-GAP-054).
- **Stores stay simple.** Each store only has to honour the id format and `keep`/`purge`, so P7 pgvector inherits
  the access control.
- **SQLite holds durable data,** so it is no longer a cache that can be wiped. Deleting it makes `acct_` collections
  unreachable but not deleted. Backups and wipes are human gates, and P7 must decide where accounts live
  (SPEC-GAP-043).
- **Tombstone rows stay forever.** They are small.
- **Ids are opaque to clients.** Clients must not assume workspace ids are 32 hex characters (SPEC-GAP-053).
- **No per-account storage quota** in P6.

## Evidence
To be added in P6: `tests/test_ownership.py`, `tests/test_anonymous_mode.py` and `tests/test_security.py` (P6
section). The new tests include `test_disabling_accounts_neither_deletes_nor_exposes_owned_workspaces`,
`test_v1_cleanup_does_not_touch_owned_collections`, `test_deleted_owned_workspace_id_is_404_for_anonymous_and_owner`
and `test_upload_racing_owner_delete_never_leaves_reachable_data`.

Read while planning:
- `src/vectorstore.py`: `cleanup_old_workspaces` filters on `collection.name.startswith("ws_")`, and `_WORKSPACE_ID`
  is `^[a-f0-9]{32}$`.
- `chromadb/api/segment.py::check_index_name`: the naming rules.

Probe on the current code (fakes): `GET /v1/workspaces` → 405 with `Allow: POST`; `/v1/auth/*` → 404; an
`Authorization` header is ignored.
