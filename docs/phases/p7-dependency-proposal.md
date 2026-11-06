# P7 T1: dependency and image proposal (PROPOSAL ONLY, nothing applied)

Author: devops. Date of checks: 2026-11-05. Scope: SPEC-GAP-064, 065, 068, 083, 085, 086, 087, 090.
Nothing in `pyproject.toml`, the lock files, compose, CI or `.venv` was changed. Scratch work lives in
`/private/tmp/claude-501/-Users-apple-rag-bot-talos-2/6cbf10fa-2111-4149-b629-e1fab384dca9/scratchpad/`.
Containers I started were short-lived, had `--network none`, and were stopped and removed (checked with `docker ps -a`).
Two images were pulled into the local Docker (colima) cache: `valkey/valkey` (196 MB) and `pgvector/pgvector` (646 MB).

Legend: **V** = verified by a command in this session; **M** = from memory, not verified.

## 1. Python packages

### 1.1 Pins

| Package | Pin | Evidence |
|---|---|---|
| `psycopg[binary]` | `psycopg[binary]==3.3.8` (pulls `psycopg-binary==3.3.8`) | V, `pip index versions psycopg` -> latest 3.3.8; PyPI JSON: uploaded 2026-11-04 |
| `redis` | `redis==8.1.0` | V, `pip index versions redis` -> latest 8.1.0; PyPI JSON: uploaded 2026-07-30 |

Freshness flag: psycopg 3.3.7 and 3.3.8 were uploaded on 2026-11-03 and 2026-11-04 (1-2 days old). 3.3.6
(2026-09-18) is the previous release and also ships cp311 wheels for all of x86_64, aarch64 and macOS arm64 (V, PyPI
JSON). If the human prefers a release that has had time to settle, use `psycopg[binary]==3.3.6`; every other statement
below was checked on 3.3.8 only.

Licences (V, PyPI metadata): psycopg and psycopg-binary are `LGPL-3.0-only`; redis is `MIT`. LGPL is a point the
human should accept: it is used unmodified as a dynamically imported library, and `psycopg-binary` bundles libpq and
OpenSSL (V: `psycopg_binary.libs/libpq-53e4e5f8.so.5.18`, `libssl-c64c0f6b.so.3` inside the aarch64 wheel).

Both require Python `>=3.10` (V), so they work with `>=3.11,<3.12`.

Proposed `pyproject.toml` diff (NOT applied; human gate, new dependencies):

```diff
     "httpx==0.28.1",
     "rank-bm25==0.2.2",
+    "psycopg[binary]==3.3.8",
+    "redis==8.1.0",
 ]
```

### 1.2 Wheels for the three target platforms (V)

Command (one per platform, into the scratchpad, not the repo):
`.venv/bin/python -m pip download --no-deps --only-binary=:all: --python-version 3.11 --implementation cp --abi cp311 --platform <plat> -d <dir> psycopg-binary==3.3.8`

| Platform | Wheel | sha256 (from `shasum -a 256` of the downloaded file) |
|---|---|---|
| linux amd64 | `psycopg_binary-3.3.8-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl` | `39bc3b42e6486ae8ace820df93b00dce4858338e8a428d14d8e71130e852bab1` |
| linux arm64 | `psycopg_binary-3.3.8-cp311-cp311-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl` | `4bedd0fbbbebe0c0f66b34c3f6be972ce38357ce3451510e1dcf5025f7011dba` |
| macOS arm64 (dev) | `psycopg_binary-3.3.8-cp311-cp311-macosx_11_0_arm64.whl` | `1126ff5eae645a304e6ca94e1c79cc19249fb54517b923ee5862badd2fabb334` |

Notes (V unless marked):
- The first try with `--platform manylinux2014_aarch64` failed ("No matching distribution"): the aarch64 wheel is tagged
  `manylinux_2_28`, not `manylinux2014`. Debian bookworm (the Dockerfile base) has glibc 2.36, so 2_28 is satisfied
  (glibc version of the base image: M; Debian 12 is documented as 2.36).
- Also published (V, PyPI JSON): macOS x86_64, musllinux, ppc64le, riscv64, win_amd64. Not needed.
- `redis` and `psycopg` are pure-Python `py3-none-any` wheels, so no per-architecture hashes are needed for them.
- All three wheel hashes above appear in the resolved lock (see 1.5).

### 1.3 Typing for `mypy --strict` (V)

- `psycopg`, `psycopg-binary` and `redis` wheels all contain `py.typed` (checked with `unzip -l`). No
  `[[tool.mypy.overrides]]` entry should be needed for import resolution.
- I ran `mypy --strict --follow-imports=silent` on a small file against the extracted wheels (via `MYPYPATH`, nothing
  installed). Result: psycopg code (`connect`, `execute`, `sql.SQL(...).format(sql.Identifier(...))`, `fetchone`)
  type-checked cleanly; `Redis.eval(...)` returns `Any`. One error: `Redis.get()` is typed `bytes | str | None` even
  with `decode_responses=True`, so
  `src/state_redis.py` must `str(...)`/`cast` replies (the plan already requires string replies). Output:
  `src/t.py:11: error: Incompatible types in assignment (expression has type "bytes | str | None", variable has type "str | None")`.
  This is a note for the backend agent, not a blocker. Real mypy on the real modules is still to be run after install.

### 1.4 Transitive dependencies and the current locks

| Dependency | Needed by | Condition | In current `requirements.lock`? | Evidence |
|---|---|---|---|---|
| `typing-extensions>=4.6` | psycopg | python < 3.13 | yes, `typing-extensions==4.16.0` | V, `grep` of both locks + PyPI `requires_dist` |
| `tzdata` | psycopg | `sys_platform == "win32"` only | yes, `tzdata==2026.5` (already present) | V |
| `async-timeout>=4.0.3` | redis | python < 3.11.3 only | **no**, new: `async-timeout==5.0.1` with marker `python_full_version < '3.11.3'` | V (PyPI `requires_dist`; added by the dry-run in 1.5) |
| `psycopg-binary==3.3.8` | psycopg `[binary]` | not pypy | no, new | V |

`async-timeout` is only installed by pip when Python is below 3.11.3; the image's Python is newer (patch version of the
pinned base digest: not checked), so it will normally be skipped, but it must still be in the lock with hashes because
`--require-hashes` applies to anything pip decides to install and the lock is universal.

### 1.5 How the locks are generated today, and the exact commands

No script, Makefile target or header documents it. Inferred (not documented): the lock is `uv pip compile` universal
output without header (`--no-header`), because (V) it contains the uv-style `# via trustrag (pyproject.toml)` lines
and two `numpy` entries split by `; python_full_version < '3.12'` markers, and has no autogenerated header.
`uv 0.12.17` is installed at `/Users/apple/.local/bin/uv`.

Dry-run proof, done only in the scratchpad (copy of `pyproject.toml` with the two new lines, copies of the current
locks as the output files so uv keeps existing pins as preferences; no install, repo untouched):

```
cd <scratch>/new
uv pip compile pyproject.toml --universal --generate-hashes --python-version 3.11 --no-header -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --generate-hashes --python-version 3.11 --no-header -o requirements-dev.lock
```

Result (V): for both files `removed lines: 0, added lines: 82`; the only new packages are `async-timeout==5.0.1`,
`psycopg==3.3.8`, `psycopg-binary==3.3.8` (with the marker `; implementation_name != 'pypy'`) and `redis==8.1.0`. The
`psycopg-binary` block has 66 hashes and contains all three wheel hashes of 1.2. Without the existing lock as
preferences, uv moved many unrelated pins (e.g. `anyio`, `filelock`, `grpcio`; 1694 diff lines), so the human **must**
run it with the existing file as `-o` target (as below) and review the diff.

Commands for the human (run in the repo root after approving the pyproject change; this is the T4 human gate):

```bash
uv pip compile pyproject.toml --universal --generate-hashes --python-version 3.11 --no-header -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --generate-hashes --python-version 3.11 --no-header -o requirements-dev.lock
git diff --stat requirements.lock requirements-dev.lock        # expect additions only
git diff requirements.lock | grep '^-' | grep -v '^---'        # expect no output (no existing pin changed)
```

Because the universal lock covers all platforms and all published wheels, the hashes for x86_64, aarch64 and macOS
arm64 are included automatically; nothing architecture-specific has to be run. SPEC-GAP-090 evidence still has to come
from after-the-fact runs: `docker buildx build --platform linux/amd64,linux/arm64 .` (CI `docker` job) and
`pip install --require-hashes --no-deps -r requirements-dev.lock` into `.venv` (human gate, not done).

## 2. Container images

All digests below are the multi-arch index digests from `docker buildx imagetools inspect <tag>` (V).

### 2.1 Postgres with pgvector

| Candidate | Digest (index) | Platforms | Evidence |
|---|---|---|---|
| `pgvector/pgvector:0.8.7-pg17-bookworm` (also tagged `pg17`, `0.8.7-pg17`, `pg17-bookworm`) | `sha256:7d0a41464797aeb9862dfdc0bbf1526b82cfd324cdd8036df19e3545f4a9ccb8` | linux/amd64, linux/arm64 | V; Docker Hub tag list shows last updated 2026-10-31 |
| `pgvector/pgvector:pg18` (alternative, floating tag) | `sha256:c29f30688a9ad961691bdf0ec1650717cc132a5814619d80f80463d6e4c42e47` | linux/amd64, linux/arm64 | V |

Recommendation: `pgvector/pgvector:0.8.7-pg17-bookworm@sha256:7d0a4146...` (PostgreSQL 17.11, pgvector 0.8.7; V from
`postgres --version` and `vector.control`). It is Debian bookworm, like the app image. The `-trixie` variants exist but
are newer Debian. pgvector's licence is the PostgreSQL licence (M; GitHub's API returns `NOASSERTION` for it).

### 2.2 Redis versus Valkey

| Candidate | Digest (index) | Platforms | Licence |
|---|---|---|---|
| `valkey/valkey:9.1.3` | `sha256:3add1a336a920ea54770a001ab6feb3da5fceea96a40373a3a2cd469b815f0b7` | linux/amd64, linux/arm64 (V) | BSD-3-Clause: V, GitHub API on `valkey-io/valkey` returned `BSD-3-Clause` |
| `valkey/valkey:8.1.11` (older line) | `sha256:8f836008e0986c36adf50da714c7060cf689dbb335f61fe077939d90fa650809` | amd64, arm64 (V) | same |
| `redis:8.2.11-bookworm` | `sha256:99ffb973380cc2f47bca06bd094f7f8c7138a0879a63cb8939896a22b7a65bba` | amd64, arm64/v8 (V) | Redis 8: AGPLv3 / RSALv2 / SSPLv1 tri-licence (M) |
| `redis:7.4.12-bookworm` | `sha256:b6b6e3dc0c0952737748e55d5d885272840c7e42ba41ab1d5489f693419882f5` | amd64, arm64/v8 (V) | RSALv2 / SSPLv1 (M) |
| `redis:7.2.17-bookworm` | `sha256:0312984fbfeb2ff1d4382ecef45b50e7d65478a6a58cf12ce28d6de7a4142e45` | amd64, arm64/v8 (V) | BSD-3-Clause, last BSD line (M) |

Licence summary:
- Verified: Valkey repo is BSD-3-Clause. The `redis/redis` repo is reported by GitHub as `NOASSERTION`, `LICENSE.txt`
  (i.e. a non-standard, multi-licence file), which is consistent with, but does not prove, the points below.
- From memory, to be confirmed by the human before relying on it: Redis up to 7.2 is BSD-3; Redis 7.4 and later
  changed to RSALv2 or SSPLv1 (source-available, not OSI open source); Redis 8 added AGPLv3 as a third option;
  Valkey is the Linux Foundation BSD-3 fork of Redis 7.2.4.
- Valkey reports `redis_version:7.2.4` in `INFO server` plus `valkey_version:9.1.3` (V), so redis-py clients that
  check the server version see a 7.2-compatible server.

Recommendation: **Valkey `9.1.3`** pinned by the digest above. It keeps a permissive licence (no AGPL/SSPL review for a
product that may be self-hosted by others) and everything the plan needs worked on it (section 3). Not verified:
that `redis-py 8.1.0` works against Valkey end to end (needs the package installed; human gate). Raw Lua `EVAL` using
`TIME`, `ZADD`, `ZCARD` worked (V). If the human wants real Redis, use `redis:7.2.17-bookworm` (BSD, but a release line
that is old) or accept AGPL with `redis:8.2.11-bookworm`. Recommending Valkey requires a one-line wording change in the
plan, SPEC-GAP-065 and ADR 0014 ("Redis" -> "Redis protocol, served by Valkey"); that is the architect's/human's edit,
not mine (ADR status changes are a human gate).

Both Docker Hub tags used above are plain tags whose OS variant I did not check for valkey (the digest is what pins it).

## 3. Feature checks (all V unless noted)

Docker client 29.8.0, daemon 29.5.2 (colima), `docker compose version` = v5.5.1, buildx v0.37.0.

| Item | Result | Evidence |
|---|---|---|
| pgvector image honours `POSTGRES_PASSWORD_FILE` | Yes | `docker-entrypoint.sh` has the `*_FILE` mechanism (line 11 `fileVar="${var}_FILE"`); functional test: container with `POSTGRES_PASSWORD_FILE=/run/pw` accepted that password and refused a passwordless TCP login after the setup below |
| `/docker-entrypoint-initdb.d` runs `*.sh` and `*.sql` | Yes | entrypoint lines 180-194; test log: `sourcing /docker-entrypoint-initdb.d/10-init.sh`; the script created extension `vector 0.8.7` and role `trustrag_app` (`rolsuper=f, rolcreatedb=f, rolcreaterole=f`). Scripts run only when the data directory is empty (M, documented image behaviour) |
| `CREATE EXTENSION vector` needs superuser | Yes: `vector.control` has no `trusted = true` | V, `cat vector.control`. Therefore the init script (running as superuser), not the app role, must create it, as SPEC-GAP-086 already says. CI test role needs the same |
| **Default image allows passwordless local connections** | **Finding.** With only `POSTGRES_PASSWORD_FILE`, `pg_hba.conf` has `trust` for the unix socket, `127.0.0.1` and `::1` (inside the container) and `scram-sha-256` for `host all all all` | V, `grep -v '^#' pg_hba.conf` and `psql -h 127.0.0.1 -w` succeeded without a password. Remote (other container) access does require the password, so SPEC-GAP-086's "trust forbidden" is met for the network, not for in-container `docker exec` |
| Fix for the finding | `POSTGRES_INITDB_ARGS="--auth-local=scram-sha-256 --auth-host=scram-sha-256"` | V: all `pg_hba.conf` lines became `scram-sha-256`; passwordless socket and TCP logins both failed with `fe_sendauth: no password supplied`; init script, password file and extension still worked. Only applies on first initialisation of an empty volume. I recommend including it |
| Valkey config `include` + `requirepass` from a separate file | Works | config `include /etc/valkey-auth.conf` (file holding `requirepass ...`) + `appendonly yes`, `appendfsync everysec`, `maxmemory 64mb`, `maxmemory-policy noeviction`: unauthenticated `PING` -> `NOAUTH Authentication required.`; `CONFIG GET` showed `maxmemory 67108864`, `noeviction`, `appendonly yes`, `appendfsync everysec` |
| `REDISCLI_AUTH` for the healthcheck | Works. `redis-cli` is a symlink to `valkey-cli` in the image, and `VALKEYCLI_AUTH` also works | `REDISCLI_AUTH=... redis-cli ping` -> `PONG` |
| Password never on a command line (compose) | Possible: file-based secret + `include`. For CI, `VALKEY_EXTRA_FLAGS` env is appended by the entrypoint (`exec "$@" $VALKEY_EXTRA_FLAGS`) | V: `VALKEY_EXTRA_FLAGS="--requirepass ci-throwaway --maxmemory 64mb --maxmemory-policy noeviction"` gave NOAUTH without auth and `PONG` with it. It exposes the password in `docker inspect` env, acceptable only for a throwaway CI value |
| Compose `internal: true` network | Accepted | `docker compose -f <scratch>.yml --profile server config` rendered `internal: true` on `backend` |
| Compose file-based `secrets:` (`file:`) | Accepted | rendered as `target: /run/secrets/pg_pw` with absolute `file:` path |
| `depends_on` with `required: false` | Supported by v5.5.1 | rendered `condition: service_started`, `required: false` for the optional dependency. Reminder (M): it only tolerates the dependency being absent/disabled by profile. A dependency with `condition: service_healthy` on a not-started profile service fails `config` ("depends on undefined service") unless `required: false` is used |
| Default `docker compose config --services` unchanged by profiled services | Yes | scratch compose with profile `server` listed only `api` without `--profile` |

Practical notes (V where stated):
- colima does not share `/private/tmp` into the VM: a bind-mounted file from the scratchpad arrived empty and Valkey
  silently ignored it (appendonly stayed `no`). Host secret files for the `server` profile should live under `$HOME`
  (colima mounts it; M for the default mount set), and the verifier should assert the config actually took effect
  (`CONFIG GET appendonly`), not just that Valkey starts.
- Valkey's entrypoint chowns its working directory to `valkey` and drops privileges (V, script text); the secret file
  must be world- or group-readable inside the container (0444, or 0440 with a matching group) because compose file
  secrets are bind mounts that keep host permissions (M). Plan text says 0600: that will not be readable by user
  `valkey` (uid not checked) unless compose `secrets:` `uid/gid/mode` long syntax is honoured for file secrets, which
  is **not verified** (Compose ignores `mode` for non-swarm file secrets, M). Decide at T3; verify then.
- Postgres runs as uid 999 `postgres` (V). Same readability remark for `POSTGRES_PASSWORD_FILE` (the entrypoint reads it
  as root before dropping privileges, M).

## 4. Proposed CI and compose outlines (text only, not applied; both are human gates)

### 4.1 `.github/workflows/tests.yml`, job `test` (add under `runs-on`)

```yaml
    services:
      postgres:
        image: pgvector/pgvector:0.8.7-pg17-bookworm@sha256:7d0a41464797aeb9862dfdc0bbf1526b82cfd324cdd8036df19e3545f4a9ccb8
        env:
          POSTGRES_PASSWORD: ci-throwaway-postgres   # CI-only value, database lives for this job only
          POSTGRES_DB: trustrag_test
          POSTGRES_INITDB_ARGS: --auth-local=scram-sha-256 --auth-host=scram-sha-256
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U postgres -d trustrag_test"
          --health-interval 5s --health-timeout 5s --health-retries 10
      redis:
        image: valkey/valkey:9.1.3@sha256:3add1a336a920ea54770a001ab6feb3da5fceea96a40373a3a2cd469b815f0b7
        env:
          VALKEY_EXTRA_FLAGS: --requirepass ci-throwaway-redis --maxmemory 64mb --maxmemory-policy noeviction
        ports: ["6379:6379"]
        options: >-
          --health-cmd "REDISCLI_AUTH=ci-throwaway-redis valkey-cli ping"
          --health-interval 5s --health-timeout 5s --health-retries 10
    env:
      TRUSTRAG_TEST_DATABASE_URL: postgresql://postgres:ci-throwaway-postgres@localhost:5432/trustrag_test
      TRUSTRAG_TEST_REDIS_URL: redis://:ci-throwaway-redis@localhost:6379/0
```

Notes: the Postgres CI role is the superuser (needed to `CREATE EXTENSION vector`, see section 3); the tests, not an
init script, create the app role and extension in the test database. The `--health-cmd` strings run inside the
containers; I verified `valkey-cli`/`REDISCLI_AUTH` locally, but the GitHub `services:` mechanics (option parsing,
health wait) were not run (no CI access). CI passwords are literals in a public workflow; gitleaks may flag the URLs, so
the human should expect an allow-list entry or a different CI value. The existing static test
`test_docker_and_ci_use_pins` needs `services.*.image` ending in `@sha256:<64 hex>`, 2 or more service images, and both
`TRUSTRAG_TEST_*` names in `env`: the block above satisfies it by reading the test (V, read, not run). The test
also requires every compose `image:` other than `trustrag:latest` to carry a digest, and keeps
`Dockerfile.count("@sha256:") == 2`, so no new digest may be added to the Dockerfile. The existing `docker` job (QEMU
multi-arch build of the app image) is unchanged and is the evidence for amd64+arm64 hashes (SPEC-GAP-090).

### 4.2 `docker-compose.yml` `server` profile outline

```yaml
services:
  # api, ui: unchanged
  postgres:
    profiles: [server]
    image: pgvector/pgvector:0.8.7-pg17-bookworm@sha256:7d0a41464797aeb9862dfdc0bbf1526b82cfd324cdd8036df19e3545f4a9ccb8
    environment:
      POSTGRES_PASSWORD_FILE: /run/secrets/pg_superuser_password
      POSTGRES_DB: trustrag
      POSTGRES_INITDB_ARGS: --auth-local=scram-sha-256 --auth-host=scram-sha-256   # no trust, even inside the container
    secrets: [pg_superuser_password, pg_app_password]
    volumes:
      - trustrag-pgdata:/var/lib/postgresql/data
      - ./docker/postgres-init.sh:/docker-entrypoint-initdb.d/10-init.sh:ro
    networks: [backend]                       # no ports:
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d trustrag"]   # pg_isready needs no password
      interval: 10s
      timeout: 5s
      retries: 10
    restart: unless-stopped
  redis:                                       # Valkey, service name kept as "redis" (test_docker_and_ci_use_pins needs it)
    profiles: [server]
    image: valkey/valkey:9.1.3@sha256:3add1a336a920ea54770a001ab6feb3da5fceea96a40373a3a2cd469b815f0b7
    command: ["valkey-server", "/usr/local/etc/valkey/valkey.conf"]
    volumes:
      - ./docker/redis.conf:/usr/local/etc/valkey/valkey.conf:ro    # template, no secret; ends with: include /run/secrets/redis_auth.conf
      - trustrag-redisdata:/data
    secrets: [redis_auth]                      # file holding one line: requirepass <password>; target redis_auth.conf
    networks: [backend]                        # no ports:
    healthcheck:
      test: ["CMD-SHELL", "REDISCLI_AUTH=\"$$(sed -n 's/^requirepass //p' /run/secrets/redis_auth.conf)\" valkey-cli ping | grep -q PONG"]
      interval: 10s
      timeout: 5s
      retries: 10
    restart: unless-stopped
  api-server-1:
    profiles: [server]
    image: trustrag:latest
    command: ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
    env_file: .env.server                      # DATABASE_URL, REDIS_URL, VECTOR_STORE=pgvector, STATE_BACKEND=redis
    ports: ["127.0.0.1:8001:8000"]
    depends_on:
      postgres: {condition: service_healthy}
      redis: {condition: service_healthy}
    networks: [backend, default]
    healthcheck: {test: ["CMD","python","-c","import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"], interval: 30s, timeout: 5s, start_period: 30s}
  api-server-2:                                # same, host port 127.0.0.1:8002
networks:
  backend:
    internal: true
volumes:
  trustrag-pgdata:
  trustrag-redisdata:
secrets:
  pg_superuser_password: {file: ${TRUSTRAG_SECRETS_DIR}/pg_superuser_password}
  pg_app_password:       {file: ${TRUSTRAG_SECRETS_DIR}/pg_app_password}
  redis_auth:            {file: ${TRUSTRAG_SECRETS_DIR}/redis_auth.conf}
```

Open points in this outline (to settle at T3, not claimed as verified):
- An `internal: true` network has no outside route, so `api-server-*` also join `default` for their published port
  `127.0.0.1:800x`; Postgres and Redis only join `backend`. Whether a published port works for a container attached to
  both an internal and a normal network on Compose v5.5.1 was not run (I only ran `config`, not `up`).
- The Redis healthcheck above reads the password from the secret file so it never appears in the compose file. Plan text says "healthcheck with
  `REDISCLI_AUTH`": that is what the command does, populated from the file.
- Secret files must live somewhere the Docker host (colima) can mount, and be readable by uid 999 / `valkey` (section 3).
- `api-server-*` use `image: trustrag:latest` (already allowed by the pins test) but would need `build: .` on only one
  of them to avoid two builds.
- `depends_on ... required: false` is supported but is not needed in this outline; it only matters if `ui`/`api` would
  depend on profiled services.
- Healthcheck `start_period`s, `maxmemory` and `restart` choices are my guesses, not measured (SPEC-GAP-083 says the
  verifier measures `INFO memory`).

## 5. Summary table

| Item | Proposed value | Verified? | Evidence |
|---|---|---|---|
| psycopg pin | `psycopg[binary]==3.3.8` (alt. 3.3.6 if the 1-day-old release is a concern) | yes | `pip index versions`, PyPI JSON upload dates |
| redis-py pin | `redis==8.1.0` | yes | `pip index versions redis`, PyPI JSON |
| Wheels amd64 / arm64 / macOS arm64 | `manylinux2014_x86_64`, `manylinux_2_28_aarch64`, `macosx_11_0_arm64` (cp311) | yes | `pip download` per platform, sha256 listed in 1.2 |
| `py.typed` | present in psycopg, psycopg-binary, redis | yes | `unzip -l` |
| mypy --strict | psycopg clean; `Redis.get` returns `bytes \| str \| None` (needs `str()`/cast) | partly (small probe, not repo code) | mypy run via `MYPYPATH` |
| Transitive deps | typing-extensions (in lock), tzdata (in lock, win32 only), async-timeout 5.0.1 (new, py<3.11.3) | yes | grep of locks, PyPI `requires_dist`, scratch compile |
| Lock command | `uv pip compile ... --universal --generate-hashes --python-version 3.11 --no-header -o <lock>` (+ `--extra dev`) | partly: generator inferred, dry run shows additions only (82 added, 0 removed lines per file) | scratch compile with existing locks as output |
| Licences of Python deps | psycopg LGPL-3.0-only, redis MIT | yes | PyPI metadata |
| Postgres image | `pgvector/pgvector:0.8.7-pg17-bookworm@sha256:7d0a41464797aeb9862dfdc0bbf1526b82cfd324cdd8036df19e3545f4a9ccb8` | yes | `buildx imagetools inspect`, `postgres --version`, `vector.control` |
| Redis-compatible image | `valkey/valkey:9.1.3@sha256:3add1a336a920ea54770a001ab6feb3da5fceea96a40373a3a2cd469b815f0b7` | yes (digest, platforms, BSD-3 via GitHub API); redis-py against Valkey not run | `imagetools inspect`, `INFO server` |
| Redis licence history | RSALv2/SSPLv1 from 7.4, AGPLv3 option in 8 | **no, from memory** | only GitHub `NOASSERTION` observed |
| `POSTGRES_PASSWORD_FILE`, initdb.d | work | yes | functional run |
| Postgres trust auth on loopback | present by default; remove with `POSTGRES_INITDB_ARGS=--auth-local=scram-sha-256 --auth-host=scram-sha-256` | yes | `pg_hba.conf` before and after, passwordless login refused after |
| Valkey `include` + `requirepass`, `REDISCLI_AUTH` | work | yes | functional run |
| Compose `internal: true`, file `secrets:`, `depends_on required:false` | accepted by v5.5.1 | yes (`config` only, not `up`) | `docker compose config` |
| Published port from container on internal + default networks | not tested | no | needs `up` in T3 |
| CI `services:` mechanics | outline only | no | no CI run |
| Secret file permissions (0600 vs uid 999) | undecided | no | see section 3 |

## 6. Needs the human's approval

1. Adding `psycopg[binary]==3.3.8` (or 3.3.6) and `redis==8.1.0` to `pyproject.toml` main `dependencies`, and accepting
   LGPL-3.0 for psycopg (SPEC-GAP-064, 090).
2. Running the two `uv pip compile` commands in section 1.5 and reviewing the lock diff; then the `.venv` install with
   `--require-hashes` (T4).
3. Image choices and digests: Postgres+pgvector 0.8.7-pg17-bookworm; Valkey 9.1.3 versus Redis (licence decision;
   confirm the Redis licence history from memory against the vendor pages) (SPEC-GAP-065); wording change in ADR 0014
   / plan for "Valkey" if chosen (ADR change is a human gate).
4. Compose `server` profile, `docker/*` files, `.env.server.example`, `.gitignore`/`.dockerignore` additions (T3).
5. CI workflow change adding `services:` and `env:` to `.github/workflows/tests.yml`, including the throwaway CI
   passwords and any gitleaks allow-list (human gate: CI workflows).
6. The `POSTGRES_INITDB_ARGS` scram setting: an extra control beyond the plan's text, none weakened.
7. Dependabot: new images in compose/CI are not tracked by the current Dependabot config unless the `docker` ecosystem
   covers compose files (not checked; M).

Security controls weakened by this proposal: none. Base images remain digest-pinned, the Dockerfile is untouched, the
install stays `--require-hashes --no-deps`. The one deviation to flag: CI service containers use password literals
(throwaway, job-local) and a password in an environment variable for Valkey in CI only; the compose profile keeps secrets
in mounted files.
