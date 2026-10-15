# Open Terminal Orchestrator

Gives every Open WebUI conversation its own [Open Terminal](https://github.com/open-webui/open-terminal)
container. Each conversation gets its own container, private network, API key and home volume.
Containers stop when idle, resume on demand, and can be wiped by the user, the AI or an admin.
They are purged after a retention period. An airgapped mode is available.

It speaks Open WebUI's built-in **orchestrator** terminal protocol, so Open WebUI needs no
fork or patch. See [docs/DESIGN.md](docs/DESIGN.md) for the design.

```
Open WebUI ──(X-Terminal-Context-Id: chat:<id>)──► orchestrator ──► oto-sbx-<uuid>  (one per chat)
                                                        │              └─ oto-vol-<uuid>-g<n> → /home/user
                                                        └─ Docker API (via socket proxy)
```

## Status

v0.1, a first version for testing.

- The unit tests (fake runtime) and a Docker integration test pass.
- The compose deployment was checked by hand in `network` mode:
  - a cold start takes about 2 s
  - a stopped container resumes in about 1.7 s
  - an `egress: none` sandbox has no network access
  - one sandbox cannot reach another
- **Verified end to end against Open WebUI v0.12.2** (32/32 checks in
  `scripts/e2e_openwebui.py`, using `openai/gpt-4.1-nano` through OpenRouter). The checks
  cover:
  - orchestrator detection, and policy and lifecycle editing through Open WebUI
  - model tool calls that land in a per-chat sandbox
  - chat-to-chat isolation, and the file sidebar
  - another user being refused a chat's sandbox
  - the interactive WebSocket terminal
  - stop and resume, and admin "Refresh Terminals"
  - the model calling `reset_sandbox`
  - chat deletion destroying the sandbox (through the event function)

## Quick start (development, orchestrator on the host)

```bash
uv sync
docker pull ghcr.io/open-webui/open-terminal:slim

ORCH_API_KEY=devkey ORCH_SANDBOX_ACCESS=published \
ORCH_DEFAULT_IMAGE=ghcr.io/open-webui/open-terminal:slim \
uv run oto serve            # listens on :8080
```

`published` mode publishes each sandbox's port on `127.0.0.1` so the orchestrator can reach it
from the host. Use it for development only. Production uses `network` mode (below).

Try it without Open WebUI:

```bash
H=(-H 'Authorization: Bearer devkey' -H 'X-User-Id: u1' -H 'X-Session-Id: c1' -H 'X-Terminal-Context-Id: chat:c1')
curl -s "${H[@]}" -H 'content-type: application/json' \
  'localhost:8080/p/default/execute?wait=10' -d '{"command":"echo hi > ~/f; ls ~"}'
uv run oto --key devkey ls
```

## Production (Docker Compose)

```bash
cd deploy
export ORCH_API_KEY=$(openssl rand -hex 32) ORCH_ADMIN_API_KEY=$(openssl rand -hex 32) ORCH_SECRET_KEY=$(openssl rand -hex 32)
docker compose up -d --build
```

- The orchestrator talks to Docker through `tecnativa/docker-socket-proxy`. It never gets
  the raw socket.
- Each sandbox gets its own `internal` network, and the orchestrator joins it only while
  the sandbox runs. Sandboxes that are allowed egress also join `oto-egress`, a bridge with
  inter-container communication disabled.
- Put Open WebUI on the `oto-webui` network (or set `OTO_WEBUI_NETWORK` to Open WebUI's
  network). Open WebUI then reaches the orchestrator at `http://oto-orchestrator:8080`.

### Airgapped

```bash
# On a connected machine
docker save open-terminal-orchestrator:latest tecnativa/docker-socket-proxy:v0.4.2 -o oto.tar
docker save ghcr.io/open-webui/open-terminal:latest -o open-terminal.tar
# On the airgapped host
docker load -i oto.tar && docker load -i open-terminal.tar
cd deploy && docker compose -f docker-compose.yml -f docker-compose.airgapped.yml up -d
```

`ORCH_AIRGAPPED=true` does the following:
- forces `pull_policy=never`
- forces `egress: none` for every policy
- rejects lifecycle updates that ask for egress

A policy whose image is not loaded shows up as `unavailable` in `oto status`.

## Open WebUI setup

1. Go to **Admin Settings → Integrations → Open Terminal** and add a connection. Set the URL to
   `http://oto-orchestrator:8080` and the key to `ORCH_API_KEY`. Click **Verify**; it should
   report **Orchestrator**.
2. Set the **Policy ID** (for example `default`), then the image, CPU, memory, storage
   (persistent) and idle timeout. The **Lifecycle** JSON takes the knobs listed below.
3. Set **Chat context → per chat (`chat_id`)**. This is what gives each conversation its
   own sandbox. Temporary chats are refused by Open WebUI in this mode.
4. Optional: paste the functions from `integrations/owui/` into **Admin → Functions**:
   - `event_chat_cleanup.py` destroys a chat's sandbox when the chat is deleted.
   - `action_reset_terminal.py` adds a "Reset terminal" button to messages.

   Set their valves to the orchestrator URL and `ORCH_ADMIN_API_KEY`.

The model also gets two tools of its own:
- `sandbox_info` reports the sandbox's state, generation, idle-stop time and purge date.
- `reset_sandbox` wipes the sandbox and needs `confirm: true`. Disable it with
  `"allow_ai_reset": false`.

## Network allowlist

API keys are the main protection. The allowlist adds a network-level layer: only Open WebUI's
backend may talk to the orchestrator, so a leaked connection key is useless from anywhere else.

```bash
ORCH_ALLOWED_CLIENTS=open-webui                    # Open WebUI's container name (or its IP/CIDR)
ORCH_ADMIN_ALLOWED_CLIENTS=open-webui,10.0.5.0/24  # optional: who may manage sandboxes (CLI, functions)
```

- Every route except `/healthz` is covered, including the WebSocket terminal and the public
  `openapi.json`. Refused requests get `403 {"detail":"Client address not allowed"}` (HTTP) or
  close code `4003` (WebSocket), and are logged at most once a minute per source.
- **Sandboxes are refused too**, because they reach the orchestrator from their own network
  addresses. This closes the "sandbox can call the orchestrator port" gap.
- Open WebUI calls the gateway, `/api/v1/policies*`, `/api/v1/status` and
  `/api/v1/terminals/refresh` itself, so it must be in `ORCH_ALLOWED_CLIENTS`. The
  `integrations/owui` functions call admin routes **from the Open WebUI process**, so include
  Open WebUI in `ORCH_ADMIN_ALLOWED_CLIENTS` too if you set that list.
- **Hostnames** are resolved at startup and every `ORCH_CLIENT_DNS_REFRESH_SECONDS`. A refused
  request triggers an immediate re-resolve (at most every 5 s), so a restarted Open WebUI
  container with a new IP keeps working.
- **Through the published port,** host-side tools (the `oto` CLI, curl) show up as the Docker
  network gateway (e.g. `172.18.0.1`). Add it to the admin list if you manage sandboxes from the
  host.
- **Reverse proxies:** the orchestrator ignores `X-Forwarded-For` unless the direct peer is in
  `ORCH_TRUSTED_PROXIES`. In that case it uses the rightmost address that isn't a trusted proxy.
  Uvicorn's own proxy-header handling is turned off so the header can't be spoofed from
  localhost.
- `GET /api/v1/status` shows the active lists and what each hostname resolved to.

## Configuration

### Environment (`ORCH_*`)

| Variable | Default | |
|---|---|---|
| `ORCH_API_KEY` | random per run | Key Open WebUI uses (connection key) |
| `ORCH_ADMIN_API_KEY` | – | Key for `/api/v1/terminals/*` (sandbox admin, functions, CLI). Strongly recommended. |
| `ORCH_SECRET_KEY` | – | Encrypts container keys and policy env values at rest |
| `ORCH_ALLOWED_CLIENTS` | – (any) | Source allowlist for every route except `/healthz`: IPs, CIDRs or hostnames, e.g. `open-webui` (the container name). See [Network allowlist](#network-allowlist). |
| `ORCH_ADMIN_ALLOWED_CLIENTS` | – (= `ORCH_ALLOWED_CLIENTS`) | If set, *replaces* the list for sandbox admin routes (`/api/v1/terminals/*` except `refresh`, and `/metrics`) |
| `ORCH_TRUSTED_PROXIES` | – | Peers whose `X-Forwarded-For` is honoured. Only for a reverse proxy in front of the orchestrator. |
| `ORCH_CLIENT_DNS_REFRESH_SECONDS` | `30` | Re-resolve hostname entries this often (also on a miss, at most every 5 s) |
| `ORCH_WEBUI_SECRET_KEY` | – | Open WebUI's `WEBUI_SECRET_KEY`; enables `auth_type: session` (the user's JWT is verified) |
| `ORCH_DATABASE_URL` | `sqlite+aiosqlite:///./data/oto.db` | SQLAlchemy async URL (Postgres: `postgresql+asyncpg://…`, install the `[postgres]` extra) |
| `ORCH_DOCKER_HOST` | local socket | e.g. `tcp://socket-proxy:2375` |
| `ORCH_SANDBOX_ACCESS` | `network` | `network` (production) or `published` (development) |
| `ORCH_SELF_CONTAINER` | `oto-orchestrator` | Orchestrator container name, joined to sandbox networks |
| `ORCH_IMAGE_ALLOWLIST` | `ghcr.io/open-webui/open-terminal*` | Comma-separated globs |
| `ORCH_PULL_POLICY` | `if-not-present` | `never`, `if-not-present` or `always-on-policy-save` |
| `ORCH_AIRGAPPED` | `false` | See above |
| `ORCH_DEFAULT_IMAGE` / `ORCH_DEFAULT_POLICY_ID` | – / `default` | Creates the default policy at startup. Unknown policy IDs fall back to it. |
| `ORCH_MAX_RUNNING_GLOBAL` | `50` | Hot sandbox cap. The least recently used idle sandbox is stopped when it is reached. |
| `ORCH_REAPER_INTERVAL_SECONDS` | `30` | |

### Policy (Open WebUI policy form)

`image`, `cpu_limit` (`"1"`, `"500m"`), `memory_limit` (`"1Gi"`), `storage`, `idle_timeout_minutes`,
`env`.

- If `storage` is unset, the policy is **ephemeral**: no volume, and data lives only as long
  as the container.
- `idle_timeout_minutes: 0` disables idle stop.

### Lifecycle JSON

| Key | Default | |
|---|---|---|
| `container_gc_hours` | 72 | Stopped → container removed, volume kept |
| `retention_days` | 30 | Volume purged this long after the last activity (`0` = keep forever) |
| `max_background_minutes` | 120 | How long running background commands may delay an idle stop |
| `start_timeout_seconds` | 60 | Health-wait budget on cold start or resume |
| `max_running` | 20 | Per-policy hot cap |
| `allow_ai_reset` | true | Expose the `reset_sandbox` tool |
| `contextless` | `reject` | `per_user` gives requests without a chat context a per-user sandbox |
| `upgrade` | `on_next_start` | Pick up a new policy image when a stopped sandbox resumes (`manual` keeps the old one) |
| `egress` | `open` | `none`, `allowlist` (+ `allowed_domains`) or `open`; forced to `none` when airgapped |
| `home_path` | `/home/user` | Volume mount point |
| `working_dir` | = `home_path` | Container working directory, which Open Terminal reports as cwd and home. `""` keeps the image default (`/app` on the slim image, not persisted). |
| `security` | see `oto/policies.py` | `no_new_privileges`, `cap_drop`, `cap_add`, `pids_limit`, `runtime` (e.g. `runsc`), `read_only_rootfs` |

## Admin CLI

```bash
export ORCH_URL=http://127.0.0.1:8080 ORCH_ADMIN_API_KEY=…
oto status                 # policies and sandbox counts
oto ls [--user U] [--state running]
oto show|stop|reset|destroy <sandbox-id>
oto policy dev --set '{"image":"ghcr.io/open-webui/open-terminal:latest","storage":"5Gi"}' --lifecycle '{"retention_days":14}'
oto gc                     # run one reaper pass now
oto audit --limit 20
```

## Tests

```bash
uv run pytest              # unit tests with an in-memory runtime (no Docker needed)
uv run pytest -m docker    # end to end against Docker; needs ghcr.io/open-webui/open-terminal:slim

# Full stack against a real Open WebUI (any OpenAI-compatible provider; OpenRouter shown)
OPENROUTER_API_KEY=sk-or-... scripts/e2e_up.sh
uv run python scripts/e2e_openwebui.py --model openai/gpt-4.1-nano
scripts/e2e_up.sh down
```

The Open WebUI run leaves a usable instance at http://127.0.0.1:13000 (`admin@e2e.local` /
`e2e-password-1`). In the chat UI, pick the "Orchestrated terminal" in the input bar to try
it by hand. A run costs well under $0.01 with `gpt-4.1-nano`.

## Known limitations

- **Tested against Open WebUI v0.12.2 only.** The orchestrator protocol is undocumented on
  Open WebUI's side. Rerun `scripts/e2e_openwebui.py` when upgrading Open WebUI.
- Small models sometimes call `run_command` without `wait` (or with `wait: "10s"`, which gets
  a 422) and answer before the output arrives. This is model behaviour, not an orchestrator
  issue.
- Volume size (`storage`) is recorded but not enforced. Docker's `local` volume driver has
  no quota.
- `/system` and `openapi.json` come from a probe container, so the prompt names the probe's
  hostname rather than the sandbox's.
- **Security defaults favour compatibility with the stock images.** `no-new-privileges` is
  off because the full image relies on `sudo`.
- **`allowlist` egress, and `none` in `published` mode, rely on Open Terminal's in-container
  iptables.** Root inside the container (sudo in the full image) can undo them. In `network`
  mode, `egress: none` is enforced by Docker (internal network) and is solid.
- **A sandbox can reach the orchestrator's port on its own network** unless
  `ORCH_ALLOWED_CLIENTS` is set. Without the list, it still needs the API key for anything
  except the public `openapi.json`, `/api/config` and `/system`. **Set the allowlist in
  production.**
- Single replica only. Locks are in-process, and the database schema is created with
  `create_all` (no migrations yet).
- Each proxied request reads its policy from the database. That is fine at small scale;
  add caching if needed.
