# Open Terminal Orchestrator — Design

Status: draft · 2026-10-15

A self-hosted control plane that gives every Open WebUI conversation its own
[Open Terminal](https://github.com/open-webui/open-terminal) container, with
persistence, idle spin-down, on-demand resume, reset, and an airgapped mode.

---

## 1. Goals and non-goals

| # | Requirement | How it is met |
|---|---|---|
| G1 | Optional airgapped deployment | No runtime pulls, image allowlist, internal-only sandbox networks, offline bundle (§9) |
| G2 | Prebuilt Docker images as the base | Policies reference pinned images. The orchestrator never builds images (§6) |
| G3 | Container per conversation, can be wiped | Sandbox keyed by `chat:<chat_id>`. Reset is available to the user, the AI and the admin (§7.4) |
| G4 | No sharing between conversations | 1 context → 1 sandbox → 1 container + 1 volume. Owner binding is enforced (§8) |
| G5 | Data persists for a period | Named volume per sandbox, deleted by a retention reaper (§7.3) |
| G6 | Spin down when idle, resume on demand | Idle reaper stops containers. The proxy starts them again on the next request (§7.2) |

Non-goals for v1: multi-host scheduling (the design leaves room for it), Kubernetes,
live migration, GPU scheduling, and building or customising images at runtime.

---

## 2. Key finding: Open WebUI already speaks an "orchestrator" protocol

Open WebUI's terminal integration (`backend/open_webui/routers/terminals.py`,
`utils/terminals.py`, `utils/tools.py`) natively supports an
**orchestrator** server type. It was built for the commercial "Terminals" product, but
it is a plain HTTP contract. If we implement that contract, **no fork of Open WebUI
and no plugin is needed for routing**:

- **Detection:** when an admin adds a system terminal connection, Open WebUI calls
  `GET {url}/api/v1/policies`. A 2xx response marks the server as `server_type: orchestrator`.
- **Policy routing:** the connection's `policy_id` makes Open WebUI call
  `{url}/p/{policy_id}/…` for every request.
- **Per-chat context:** when the connection's chat context is set to `context_id: chat_id`,
  every request that comes from a chat carries:
  - `X-Terminal-Context-Id: chat:<chat_id>` (only for *saved* chats; temporary
    chats are refused by Open WebUI with 409)
  - `X-User-Id: <owui user id>`
  - `X-Session-Id: <chat_id>` (Open Terminal also uses this for cwd tracking)
  - `Authorization: Bearer <connection key>`. With `auth_type: session` it is the user's
    Open WebUI JWT instead.

  Automations get `automation:<automation_id>` the same way.
- **The context header is attached on every path:** AI tool calls (`get_terminal_tools`
  → `execute_tool_server`), the file-browser sidebar (`/terminals/{id}/chats/{chat_id}/…`),
  the AGENTS.md and skills loading, and the interactive WebSocket
  (`/p/{policy}/api/terminals/{sid}`).
- **Requests without context:** Open WebUI fetches `openapi.json` with no user headers,
  and `/api/config` and `/system` with `X-User-Id: system` and no context. It caches the
  results. These requests must **not** start a container (§5.3).
- **Admin API:** Open WebUI's admin UI proxies the following calls:
  - `GET/PUT /api/v1/policies/{id}` with fields `image`, `cpu_limit`, `memory_limit`,
    `storage`, `idle_timeout_minutes` and `env`
  - `GET/PUT /api/v1/policies/{id}/lifecycle` with a free-form JSON body
  - `POST /api/v1/terminals/refresh` with body `{user_id?, policy_id?, only_idle, reset}`

  Its user-facing proxy blocks `api/v1/policies`, `api/v1/status` and
  `api/v1/terminals`, so end users cannot reach the admin API through it.

**Design decision:** implement this contract exactly. Everything else (reset tool,
chat-deletion cleanup) is added on top of it, so it does not break when Open WebUI upgrades.

> Risk: this contract is undocumented and may change. Mitigations: pin a supported
> Open WebUI version range, keep contract tests built from captured requests (§12),
> and keep the protocol adapter isolated in one module.

---

## 3. Architecture

```
 Browser ──► Open WebUI ──(system terminal connection, server_type=orchestrator)──┐
             │  event fn: chat.deleted ──► admin API                              │
             ▼                                                                    ▼
 ┌──────────────────────────── Orchestrator (single process) ──────────────────────────┐
 │  Gateway (HTTP + WS reverse proxy)                                                  │
 │    ├─ Auth: connection key / OWUI JWT                                               │
 │    ├─ Context resolver: X-Terminal-Context-Id → sandbox                             │
 │    ├─ Spec/static responder (openapi.json, /api/config, /system)                    │
 │    └─ Orchestrator tools (/_sandbox/reset, /_sandbox/info)                          │
 │  Sandbox Manager (state machine, per-sandbox single-flight locks)                  │
 │  Reaper (idle stop, container GC, retention purge, capacity eviction)               │
 │  Admin API (/api/v1/policies, /lifecycle, /terminals/refresh, /status, /sandboxes)  │
 │  Store (SQLite by default; Postgres optional)    Runtime driver (Docker API)        │
 └─────────────────────────────────────────┬───────────────────────────────────────────┘
                                           │ Docker Engine API (via socket proxy)
             ┌─────────────────────────────┼─────────────────────────────┐
        net-sbx-a1 (internal)         net-sbx-b2 (internal)        …
        ┌───────────────┐             ┌───────────────┐
        │ open-terminal │             │ open-terminal │   one container per conversation
        │  :8000        │             │  :8000        │   random API key per container
        └──────┬────────┘             └──────┬────────┘
          vol-sbx-a1 → /home/user        vol-sbx-b2 → /home/user
```

Stack: Python 3.12, FastAPI, `httpx` and `websockets` for proxying, `aiodocker` for the
runtime, SQLAlchemy plus Alembic for storage. This is the same ecosystem as Open WebUI and
Open Terminal, which makes the code easy for contributors to read and easy to vendor for
offline installs.

---

## 4. Domain model

```
Policy      id, image (digest-pinned), cpu_limit, memory_limit, storage, env{},
            idle_timeout_minutes, lifecycle{…see §7…}, created_at, updated_at

Sandbox     id (uuid), context_id ("chat:…" | "automation:…"), policy_id,
            owner_user_id, generation (int, ++ on reset), state,
            container_id?, volume_name, network_name, api_key (encrypted at rest),
            image_digest, created_at, last_active_at, last_started_at,
            stopped_at?, purge_after?
            UNIQUE(context_id, policy_id) WHERE state != 'destroyed'

AuditEvent  ts, sandbox_id, actor (user|ai|admin|system), action, detail
```

The context string is the routing key, and the sandbox UUID is the identity. Docker objects
are named from the UUID, for example `oto-<uuid>` and `oto-vol-<uuid>-g<generation>`, and
labelled `oto.sandbox=<uuid>`, `oto.context=<context_id>`, `oto.policy=…` and
`oto.generation=…`. Docker can therefore always be reconciled with the database (§10).

---

## 5. Request path

### 5.1 Route table (what the orchestrator serves)

| Route | Caller | Behaviour |
|---|---|---|
| `GET /api/v1/policies` | OWUI verify, admin | List policies. Also serves as orchestrator detection |
| `GET/PUT /api/v1/policies/{id}` | OWUI admin UI | Policy CRUD. A PUT is validated against the image allowlist |
| `GET/PUT /api/v1/policies/{id}/lifecycle` | OWUI admin UI | Lifecycle knobs (§7) |
| `POST /api/v1/terminals/refresh` | OWUI admin UI | Recreate containers (`reset=false`: keep volume; `reset=true`: wipe). Filter by `user_id`, `policy_id` and `only_idle` |
| `GET /api/v1/status`, `/api/v1/sandboxes[/{id}]`, `DELETE /api/v1/sandboxes/by-context/{ctx}` | admin, event fn | Ops and cleanup |
| `GET /p/{policy}/openapi.json` | OWUI (no context) | Cached Open Terminal spec **plus** the orchestrator tools (§7.4) |
| `GET /p/{policy}/api/config`, `/system` | OWUI (no context) | Served from the policy's spec cache |
| `POST /p/{policy}/_sandbox/reset`, `GET …/_sandbox/info` | AI tool call, Action fn | Orchestrator tools |
| `* /p/{policy}/{path}` | OWUI (with context) | Resolve sandbox → ensure running → reverse-proxy |
| `WS /p/{policy}/api/terminals/{sid}` | OWUI WS proxy | Same steps, plus a swap of the first-message auth |
| `GET /healthz`, `/metrics` | ops | Liveness and Prometheus metrics |

### 5.2 Proxied request flow

1. **Authenticate.** With `auth_type: bearer`, compare the token to the connection key
   in constant time. With `auth_type: session`, verify the Open WebUI JWT using the shared
   `WEBUI_SECRET_KEY` and check that the token's user matches `X-User-Id`.
2. **Resolve the context.** If `X-Terminal-Context-Id` is missing, return 409 with a clear
   message, except for the context-free routes in §5.3. The policy's
   `lifecycle.contextless` setting can instead allow a per-user fallback sandbox
   (`user:<id>`). The default is `reject`.
3. **Look up or create the sandbox** for `(context_id, policy_id)` under a per-key
   async lock (single-flight). If ten requests arrive for a cold chat, one start happens
   and the other nine wait on it.
4. **Check the owner** (§8). On first creation, bind `owner_user_id = X-User-Id`. Later
   requests with a different user get 403.
5. **Ensure the sandbox is running** (§7.1). This waits for `GET /health` on the
   container, with a budget of `start_timeout_seconds` (default 60).
6. **Forward the request.** Strip the hop-by-hop headers, `Authorization` and
   `X-Terminal-Context-Id`. Inject `Authorization: Bearer <container api key>` and keep
   `X-Session-Id`. Stream request and response bodies, so uploads, `/files/view` and the
   `/proxy/{port}` app preview work without buffering.
7. **Touch activity.** Update `last_active_at` in memory and flush it to the database at
   most every 15 s.

The WebSocket path: read the client's first `{"type":"auth"}` frame and validate it as in
step 1. Connect upstream and send `{"type":"auth","token":<container key>}`, then pipe
frames in both directions. While a WebSocket is open, the sandbox counts as **pinned
active** and is never idled.

### 5.3 Context-free requests (spec cache)

Open WebUI loads tool specs and system prompts once per connection and caches them, with no
chat context. Starting a container for these calls would be wasteful and racy. Instead,
when a policy is created or changed, the orchestrator does the following:

1. It starts a throwaway **probe container** from the policy image. The probe has no
   volume and no network egress.
2. It fetches `openapi.json`, `/api/config` and `/system`, and stores them keyed by
   `image_digest`.
3. It merges the orchestrator tool operations into the spec. It also rewrites
   `/system`, adding one line about the reset tool and the persistence and idle behaviour.
4. It destroys the probe.

These responses come from that cache. A policy's image cannot go live until its probe has
passed, which also validates the image early.

---

## 6. Images

- A policy's `image` must match the **allowlist** (`ORCH_IMAGE_ALLOWLIST`, globs over
  `repo:tag` or `repo@sha256:…`). On save, the orchestrator resolves the tag to a digest
  and stores the digest, so a later re-tag never silently changes running sandboxes.
- The pull policy (`ORCH_PULL_POLICY`) is one of `never` (airgapped default),
  `if-not-present`, or `always-on-policy-save`. Pulls are never triggered by a user request.
- Recommended base images are `ghcr.io/open-webui/open-terminal:<version>`, or a derived
  image built in CI. Do **not** rely on `OPEN_TERMINAL_PACKAGES` / `PIP` / `NPM` env
  installs. They run on every start, slow down cold starts, and fail offline. Bake those
  dependencies into a derived image instead (`FROM ghcr.io/open-webui/open-terminal:x.y`).
- **Image upgrades:** sandboxes keep their `image_digest` until they are recreated. The
  policy lifecycle setting `upgrade: on_next_start | manual` decides whether a stopped
  sandbox picks up the new digest when it resumes. The home volume survives either way.

---

## 7. Lifecycle

### 7.1 Sandbox state machine

```
             create                 healthy
  (none) ───────────► CREATING ─────────────► RUNNING ◄──────────┐
                          │ fail                 │ idle / evict   │ request (resume)
                          ▼                      ▼                │
                       FAILED               STOPPING ──► STOPPED ─┤
                                                             │    │
                                   container_gc_after        ▼    │
                                                        DEHYDRATED┘  (container removed,
                                                             │        volume kept; resume
                                   retention expired         │        = create container
                                   / chat deleted / reset    ▼        on existing volume)
                                                         DESTROYED (volume removed)
```

There are three tiers of persistence:

| Tier | State | What survives | Resume cost |
|---|---|---|---|
| Hot | RUNNING | Everything, including processes | 0 |
| Warm | STOPPED (`docker stop`) | The whole container filesystem, including `apt install`ed packages, plus the volume | ~1–3 s |
| Cold | DEHYDRATED (`docker rm`, volume kept) | Only `/home/user` (the volume) | ~2–5 s |

### 7.2 Idle spin-down

The reaper runs every 30 s. A RUNNING sandbox is stopped when **all** of these are true:

- `now - last_active_at > idle_timeout_minutes` (policy field, default 30)
- no open WebSocket (pinned active)
- no background processes, or the grace period has passed. The reaper calls the
  container's `GET /execute` to list processes. A running job extends the idle deadline,
  up to `lifecycle.max_background_minutes` (default 120), so a long build is not killed
  mid-run but a forgotten `sleep infinity` cannot keep a sandbox alive forever.

Stopping uses `docker stop -t 10`.

**Capacity guard:** `lifecycle.max_running` is set per policy, and
`ORCH_MAX_RUNNING_GLOBAL` applies to the whole host. When a start would exceed a limit, the
least recently active sandbox without an open WebSocket is stopped first (LRU). If none can
be evicted, the request gets a 503 with `Retry-After`.

### 7.3 Retention

| Knob (`lifecycle` JSON) | Default | Effect |
|---|---|---|
| `idle_timeout_minutes` (policy field) | 30 | RUNNING → STOPPED |
| `container_gc_hours` | 72 | STOPPED → DEHYDRATED (frees the container's writable layer) |
| `retention_days` | 30 | Time after last activity before the volume is purged (→ DESTROYED) |
| `max_background_minutes` | 120 | Cap on how long background processes can extend idling |
| `start_timeout_seconds` | 60 | Health-wait budget on resume |
| `max_running` | 20 | Per-policy hot cap |
| `allow_ai_reset` | true | Expose the reset tool to the model |
| `contextless` | `reject` | `reject` \| `per_user` |
| `upgrade` | `on_next_start` | See §6 |

A purge is always logged to the audit log. An admin can also set `retention_days: 0` to mean
"keep forever".

### 7.4 Reset ("wipe and start fresh")

A reset destroys the container and the volume, increments `generation`, creates a fresh
volume, and starts the sandbox (or leaves it stopped, if `start: false` is set). The sandbox
UUID, owner and context binding stay the same. There are four entry points:

1. **AI.** The orchestrator injects two operations into the policy's
   `openapi.json`:
   - `reset_sandbox` (`POST /_sandbox/reset`, body `{confirm: true, reason: str}`)
   - `sandbox_info` (`GET /_sandbox/info`), which returns the generation, the image,
     disk usage, time to idle and time to purge

   Open WebUI turns these into model tools automatically, and its tool-approval feature can
   require the user to click to confirm. `allow_ai_reset: false` removes them from the spec.
2. **User.** The user asks the AI ("reset my terminal"), which goes through path 1. As an
   option, a small Open WebUI **Action function** (shipped in `integrations/owui/`) adds a
   "Reset terminal" button to messages. It calls `POST /api/v1/sandboxes/by-context/chat:<id>/reset`
   with an admin key held in the function's valves. It also checks that `__user__` owns
   the chat before calling.
3. **Admin.** The Open WebUI admin UI's "Refresh Terminals" button with *Reset* ticked calls
   `/api/v1/terminals/refresh`, or the admin can call the API or CLI directly.
4. **Chat deleted.** An Open WebUI **Event function** listens for `chat.deleted` and
   `chat.deleted_all` and calls `DELETE /api/v1/sandboxes/by-context/chat:<id>`, which
   destroys the sandbox immediately. If functions are disabled, the retention reaper
   collects these sandboxes anyway, just later.

---

## 8. Isolation and security

**Between conversations (G4):**
- Each sandbox has its own container, its own volume, its own **internal** Docker network,
  and its own random API key. No sandbox can reach another sandbox, and no sandbox can
  reach Open WebUI.
- **Owner binding.** Open WebUI's `/terminals/{id}/chats/{chat_id}/…` proxy takes
  `chat_id` from the URL and does not check that the user owns that chat. Without a check,
  user A could address user B's sandbox by guessing a chat ID. The orchestrator therefore
  binds each sandbox to the first `X-User-Id` it sees and rejects any other user with 403.
  Admins can transfer ownership through the admin API.
- **Shared and cloned chats.** A shared chat link is read-only in Open WebUI, and the
  owner check blocks the viewer anyway. A cloned chat gets a new chat ID, so it gets a new,
  empty sandbox. A later option, `clone_mode: copy_volume`, could copy the volume instead;
  it is out of scope for v1.

**Container hardening (defaults; each can be overridden per policy):**
- `--security-opt no-new-privileges`, `--cap-drop ALL` plus the minimal set Open Terminal's
  entrypoint needs, `--pids-limit 512`, and the policy's CPU and memory limits. Disk is
  limited through the volume size where the storage driver supports it (xfs+pquota or a
  loopback volume driver); otherwise a soft quota is enforced by the reaper, which reads
  `du` from `/_sandbox/info` data.
- Optional `runtime: runsc` (gVisor) per policy, for stronger kernel isolation. It works
  offline.
- No host bind mounts. The Docker socket is never passed into a sandbox. (The default
  Open Terminal image contains the Docker CLI, which is harmless without a socket.)

**Orchestrator ↔ Docker:**
- Talk to Docker through a socket proxy such as `tecnativa/docker-socket-proxy`, limited to
  the endpoints for containers, volumes, networks, images (read-only) and exec. Rootless
  Docker or Podman is a supported alternative.
- Act only on objects labelled `oto.managed=true`. Never touch anything else on the host.

**Networking model:**
- For each sandbox, create a network `oto-net-<uuid>` with `internal: true` (no route to
  the outside world). While the sandbox is running, the orchestrator container is connected
  to that network and reaches the sandbox at `oto-<uuid>:8000`. On stop, it disconnects.
  Configure Docker's `default-address-pools` with small subnets (for example /28 out of
  10.200.0.0/16, which gives about 4k networks) so the number of networks never runs out.
- **Egress (not airgapped):** policy `egress: none | allowlist | open`.
  - `none`: no extra network is attached.
  - `allowlist`: Open Terminal's built-in `OPEN_TERMINAL_ALLOWED_DOMAINS` (DNS allowlist
    plus iptables, which needs `NET_ADMIN`) is used, together with a shared egress network
    with ICC disabled.
  - `open`: the shared egress network with ICC disabled is attached.

  ICC-disabled networks keep sandboxes from reaching each other even when they share one.

**Secrets:** container API keys and connection keys are encrypted at rest with a key from
`ORCH_SECRET_KEY`. Policy `env` values are stored encrypted and never returned in full by
`GET` (they are masked).

---

## 9. Airgapped mode (G1)

Setting `ORCH_AIRGAPPED=true` turns on the following:

- `ORCH_PULL_POLICY=never`. Images must already be present (`docker load -i bundle.tar`).
  On startup, any policy whose image digest is missing is reported as `unavailable`.
- Egress is forced to `none` for all policies, and policy updates that request otherwise are
  rejected.
- No outbound calls from the orchestrator: no update checks, no telemetry, and
  no remote JWKS. The Python dependencies are vendored as wheels in the release bundle.
- **Release bundle** (`make bundle`) produces:
  - `orchestrator.tar` (the image)
  - `open-terminal-<ver>.tar` (one or more sandbox images)
  - `wheels/`
  - `docker-compose.airgapped.yml`
  - `SHA256SUMS`

  An optional internal registry mirror can be used instead of tarballs, through the
  allowlist plus `if-not-present`.
- Open WebUI side: point the system terminal connection at the orchestrator's internal URL.
  Nothing else changes.

---

## 10. Reliability

- **Startup reconcile:** list Docker objects labelled `oto.managed=true` and reconcile them
  with the database.
  - Containers or volumes that the database doesn't know about are adopted by their labels,
    or quarantined (stopped, never deleted) if the labels are invalid.
  - Database rows whose container is missing become DEHYDRATED if the volume exists, and
    FAILED if it doesn't.
  - Stuck `CREATING` and `STOPPING` rows are retried.
- **Docker events stream:** subscribe to die and oom events and mark those sandboxes
  STOPPED. The next request resumes them, and an OOM kill is reported to the user as a 503
  with the reason.
- **Idempotent operations:** every Docker mutation is keyed by a deterministic name, so a
  retry after a crash finds the object instead of creating a duplicate.
- **Concurrency:** use per-sandbox async locks within the process. v1 runs a single
  orchestrator replica; for HA, swap the locks for Postgres advisory locks
  (`pg_advisory_lock(hash(sandbox_id))`). The design keeps every piece of state in the
  database or in Docker labels, so this is the only change needed.
- **Timeouts:** Open WebUI's proxy has a 300 s total timeout and a 10 s connect timeout.
  Cold start plus the request must fit inside that, and the health wait is capped by
  `start_timeout_seconds`. Pre-pulled images keep resume times in seconds.

---

## 11. Observability and operations

- Prometheus metrics:
  - `oto_sandboxes{state,policy}`
  - `oto_start_seconds` (histogram, cold vs. warm)
  - `oto_proxy_requests_total{code}`
  - `oto_evictions_total`, `oto_purges_total`
  - `oto_volume_bytes`
- Structured JSON logs that carry `sandbox_id`, `context_id` and `user_id`.
- An audit log table for create, reset, purge, owner transfer and policy change.
- CLI: `oto sandboxes ls|inspect|stop|reset|purge`, `oto policies …`, `oto gc --dry-run`.

---

## 12. Configuration (environment)

```
ORCH_LISTEN=0.0.0.0:8080
ORCH_API_KEY=…                 # key Open WebUI uses (bearer auth) — also admin API key
ORCH_ADMIN_API_KEY=…           # optional separate key for /api/v1/sandboxes etc.
ORCH_WEBUI_SECRET_KEY=…        # only for auth_type=session
ORCH_SECRET_KEY=…              # at-rest encryption
ORCH_DATABASE_URL=sqlite:////data/oto.db
ORCH_DOCKER_HOST=tcp://socket-proxy:2375
ORCH_IMAGE_ALLOWLIST=ghcr.io/open-webui/open-terminal:*,registry.local/terminals/*
ORCH_PULL_POLICY=if-not-present
ORCH_AIRGAPPED=false
ORCH_MAX_RUNNING_GLOBAL=50
ORCH_SELF_CONTAINER=oto-orchestrator   # name used to join sandbox networks
```

**Open WebUI admin setup:**
1. Go to Admin → Integrations → Open Terminal → add a connection with URL
   `http://orchestrator:8080` and the key.
2. Click Verify. It should report "Orchestrator".
3. Set the policy ID, image and limits.
4. Set **Chat context → per chat (`chat_id`)**.
5. Optionally import the Event function (chat cleanup) and the Action function (reset
   button) from `integrations/owui/`.

**Contract tests:** run a pinned Open WebUI container in CI. Drive one chat through it (a
tool call, the file sidebar and the WebSocket terminal) and check that the expected headers
reach a recording upstream. This catches protocol drift on Open WebUI upgrades.

---

## 13. Proposed repository layout

```
oto/
  api/           admin.py  gateway.py  ws.py  orchestrator_tools.py
  core/          manager.py (state machine)  reaper.py  locks.py  spec_cache.py  policies.py
  runtime/       base.py (Runtime protocol)  docker.py   # podman/k8s later
  store/         models.py  migrations/
  security/      auth.py  crypto.py
  cli.py  config.py  main.py
integrations/owui/  event_chat_cleanup.py  action_reset_terminal.py
deploy/          docker-compose.yml  docker-compose.airgapped.yml  socket-proxy.yml
tests/           unit/  integration/ (real Docker)  contract/ (pinned Open WebUI)
docs/DESIGN.md
```

`runtime/base.py` defines the `Runtime` protocol: `ensure_volume`, `create`, `start`,
`stop`, `remove`, `inspect`, `connect_network`, `exec_health` and `events`. This is the seam
for a later Kubernetes driver (Pod + PVC per sandbox).

---

## 14. Delivery plan

| Phase | Scope | Exit criteria |
|---|---|---|
| 0 | Contract spike: record real Open WebUI → orchestrator traffic | Fixture set of the headers and paths for tool calls, sidebar, WS and spec load |
| 1 | Gateway, Docker runtime, create/start/proxy, SQLite, spec cache | A chat runs `ls` through Open WebUI. Two chats see different filesystems |
| 2 | Reaper (idle stop, GC, retention), LRU capacity, resume | A sandbox resumes with files intact after idle stop and after dehydrate |
| 3 | Reset (AI tool, admin refresh), Event and Action functions, owner binding | Chat delete purges the sandbox. A cross-user access attempt gets 403 |
| 4 | Hardening: socket proxy, per-sandbox internal networks, gVisor option, egress modes | Security test suite passes |
| 5 | Airgapped bundle, metrics, CLI, docs | Fresh install on a host with no network passes the end-to-end run |

---

## 15. Open questions

1. **Admin key versus per-user auth.** Bearer auth with one shared key is simplest.
   `auth_type: session` lets the orchestrator verify the user cryptographically instead of
   trusting `X-User-Id`, but it needs Open WebUI's secret. Recommendation: bearer by
   default, with session auth as an option for deployments where the network between Open
   WebUI and the orchestrator isn't trusted.
2. **Automations.** Should `automation:<id>` contexts use the same lifecycle? The
   recommendation is yes, but with a shorter idle timeout and no WebSocket pinning.
3. **Per-user quotas** (the number of sandboxes or total GB per user) are not covered in v1;
   the reaper could enforce them later.
4. **Licensing.** This design reimplements a protocol and copies no code from the
   commercial "Terminals" product. Before any public release, confirm that this is
   acceptable.

---

## 16. Implementation status (v0.1)

Implemented in `oto/` and covered by `tests/`:

| Design section | Module |
|---|---|
| §2 contract: policies, lifecycle, refresh, status, `/p/{policy}` routing, WS | `api/admin.py`, `api/gateway.py` |
| §4 data model | `db.py` (SQLite/Postgres via SQLAlchemy; `create_all`, no migrations yet) |
| §5.2 proxy: single-flight start, owner binding, header rewrite, streaming, retry once on a dead upstream | `api/gateway.py`, `manager.py` |
| §5.3 spec cache via a probe container | `spec_cache.py` |
| §6 allowlist, digest pinning, pull policy | `policy_store.py`, `runtime/docker.py` |
| §7 state machine, idle stop (plus background-process grace and WS/in-flight pins), LRU capacity, GC, retention, reset (AI/user/admin/chat-deleted) | `manager.py`, `reaper.py`, `integrations/owui/` |
| §8 per-sandbox internal network, ICC-off egress network, per-sandbox keys, encrypted secrets | `manager.py`, `crypto.py` |
| §9 airgapped switches and compose overlay | `config.py`, `deploy/` |
| §10 startup reconcile (adopt, quarantine orphans, remove stray probes), crash detection | `manager.reconcile`, `reaper.py` |
| §11 audit log, `/metrics`, CLI | `db.py`, `main.py`, `cli.py` |

Where v0.1 differs from the design:

- **Admin endpoints for integrations** are under `/api/v1/terminals/*` rather than
  `/api/v1/sandboxes`. Open WebUI's user-facing proxy refuses that prefix, so a user can never
  reach them through Open WebUI, even with a connection that has no policy ID.
- **`published` access mode** was added for development (orchestrator on the host). Sandbox
  networks are not `internal` in that mode.
- **Ephemeral policies** (no `storage`) follow Open WebUI's form semantics. They have no
  volume and are destroyed, not dehydrated, at container GC time.
- **Crash detection** inspects running containers on each reaper tick instead of
  subscribing to the Docker event stream.
- **Security defaults** keep the stock Open Terminal images working: `no-new-privileges`
  off, and a small `cap_drop` set. Tighten them per policy.
- **The container working directory defaults to `home_path`.** Live testing showed the slim
  image runs from `/app`, which Open Terminal then reports as cwd and home. Relative paths and
  the file browser therefore landed outside the persisted volume.
- **Contract tests:** `scripts/e2e_openwebui.py` drives a real Open WebUI (v0.12.2 verified)
  over HTTP and WebSocket. It is not wired into CI yet, because it needs an LLM provider key.
- **Source-address allowlist (added after v0.1):** `ORCH_ALLOWED_CLIENTS` and
  `ORCH_ADMIN_ALLOWED_CLIENTS` (IPs, CIDRs or hostnames) and `ORCH_TRUSTED_PROXIES`, enforced
  by ASGI middleware for HTTP and WebSocket (`client_filter.py`). With it set, sandboxes can no
  longer reach the orchestrator API at all. The live test runs with the stack restricted to
  the Open WebUI container: 32/32 checks pass, and the host, other containers and a sandbox get
  403.
- **Not built yet:** Postgres advisory locks (HA), Alembic migrations, volume quotas, and the
  release `make bundle`.
