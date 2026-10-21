# Open WebUI + Open Terminal Orchestrator

A single Compose file that runs Open WebUI with a private terminal sandbox for every chat.
The terminal connection is pre-registered, so it works without any setup in the UI.

Already running Open WebUI with Compose? Use [`examples/add-to-existing`](../add-to-existing)
instead.

| Service | Purpose |
|---|---|
| `open-webui` | Open WebUI on http://localhost:3000 |
| `orchestrator` | Creates, stops and wipes one Open Terminal container per chat |
| `socket-proxy` | Limited Docker API access for the orchestrator (no raw socket) |

Requirements: Docker with Compose v2, on a host where this stack may create containers.
Only one orchestrator should run per Docker host.

## Start

```bash
cp .env.example .env
for v in WEBUI_SECRET_KEY ORCH_API_KEY ORCH_ADMIN_API_KEY ORCH_SECRET_KEY; do
  echo "$v=$(openssl rand -hex 32)"
done > .env
docker compose up -d
```

1. Open http://localhost:3000 and create the first account. It becomes the admin.
2. Add a model provider under **Admin Settings → Connections**, or set `OPENAI_API_BASE_URL`
   and `OPENAI_API_KEY` in `.env` before the first start.
3. In a chat, pick **Terminal** in the input bar. The model can now run commands, and the
   file browser shows the chat's sandbox.

The first sandbox image (about 6 GB for the full image) is pulled in the background when the
orchestrator starts. A chat that asks for a terminal before the pull finishes waits for it.

To make the terminal the default, edit a model in **Admin Settings → Models** and set
**Default terminal** to "Terminal". New chats with that model start with it selected.

## What users get

- **Isolation:** each chat has its own container and volume. Chats never share them.
- **Idle and resume:** idle sandboxes stop after 30 minutes and start again on the next
  request, with files kept.
- **Reset:** the model can wipe its own sandbox with the `reset_sandbox` tool.
- **Retention:** a stopped sandbox's container is removed after 3 days (its files stay) and
  its data is purged after 30 days without use. Change these
  under **Admin Settings → Integrations → Open Terminal → Lifecycle**.

## Options (`.env`)

| Variable | Default | |
|---|---|---|
| `SANDBOX_IMAGE` | `ghcr.io/open-webui/open-terminal:latest` | Use `ghcr.io/felix920506/open-terminal-scientific:latest` for the full scientific Python and Excel stack |
| `OPEN_WEBUI_PORT` | `3000` | Host port for Open WebUI |
| `MAX_RUNNING_SANDBOXES` | `20` | Running sandboxes at once; the least recently used one is stopped beyond this |
| `OPEN_WEBUI_IMAGE` | `ghcr.io/open-webui/open-webui:v0.12.2` | Tested with 0.11.4 and 0.12.2 |
| `OTO_IMAGE` | `ghcr.io/felix920506/open-terminal-orchestrator:0.1.1` | |

The terminal connection in `TERMINAL_SERVER_CONNECTIONS` is applied only on Open WebUI's first
start. After that, edit it under **Admin Settings → Integrations → Open Terminal**. If you change
`ORCH_API_KEY` later, update the key there too.

### Changing the sandbox image

Change `SANDBOX_IMAGE` in `.env` and run `docker compose up -d`. The orchestrator switches the
`default` policy to the new image. Each chat's sandbox moves to it the next time the sandbox
starts, and its files are kept. Check the result with:

```bash
docker exec oto-orchestrator oto status   # policies → "image" and "status": "ready"
```

- On an airgapped host, `docker load` the image first, under exactly the name in
  `SANDBOX_IMAGE`. Until then the policy shows `"status": "unavailable"`.
- If an admin has set a different image in Open WebUI's policy editor, that choice wins and
  `SANDBOX_IMAGE` is ignored (the orchestrator logs a warning). Change it there instead, or set
  it back to the previous `SANDBOX_IMAGE` value to let `.env` manage it again.

## Optional: chat cleanup and a reset button

Paste the functions from [`integrations/owui/`](../../integrations/owui) into **Admin Settings →
Functions**, then set their `admin_api_key` valve to `ORCH_ADMIN_API_KEY` from `.env`. The
default URL (`http://oto-orchestrator:8080`) already matches this stack.
- `event_chat_cleanup.py` deletes a chat's sandbox when the chat is deleted.
- `action_reset_terminal.py` adds a "Reset terminal" button to messages.

## Administration

```bash
docker exec oto-orchestrator oto status     # policies, image status, running count
docker exec oto-orchestrator oto ls         # sandboxes per chat
docker exec oto-orchestrator oto reset <id> # wipe one sandbox
```

## Remove everything

Sandboxes are created by the orchestrator, not by Compose, so delete them first:

```bash
docker exec oto-orchestrator sh -c 'oto ls --json | python -c "import json,sys; [print(b[\"id\"]) for b in json.load(sys.stdin)]" | xargs -rn1 oto destroy'
docker compose down -v
docker network rm oto-egress
```
