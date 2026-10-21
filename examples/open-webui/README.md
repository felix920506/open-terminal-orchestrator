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
3. In a chat, pick **Terminal** in the input bar's terminal menu, under **System**. The model
   can now run commands, and the file browser shows the chat's sandbox.

   The terminal is already connected; don't add it again. In particular, don't add it under
   your personal **Settings → Integrations → Open Terminal**. That page looks like the admin
   one, but it creates a **Direct** terminal that your browser calls itself. It cannot reach
   the orchestrator, and it sends no chat ID.

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
| `SANDBOX_IMAGE` | `ghcr.io/open-webui/open-terminal:latest` | Use `ghcr.io/felix920506/open-terminal-scientific:latest` for scientific Python, image processing and Excel, PDF, Word and PowerPoint handling ([contents](../../README.md#sandbox-images)) |
| `OPEN_WEBUI_PORT` | `3000` | Host port for Open WebUI |
| `MAX_RUNNING_SANDBOXES` | `20` | Running sandboxes at once; the least recently used one is stopped beyond this |
| `STAGE_UPLOADS` | `false` | Hold files attached before a chat's first message; see [below](#optional-attach-files-before-the-first-message) |
| `OPEN_WEBUI_IMAGE` | `ghcr.io/open-webui/open-webui:v0.12.2` | Tested with 0.11.1, 0.11.4 and 0.12.2 |
| `OTO_IMAGE` | `ghcr.io/felix920506/open-terminal-orchestrator:0.1.5` | |

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

`filter_terminal_uploads.py` is installed differently; see the next section.

## Optional: attach files before the first message

With **Chat Uploads: Filesystem** on the terminal connection, files you attach in a chat go
straight into that chat's sandbox instead of Open WebUI's document store. Open WebUI uploads
an attachment as soon as you pick it, though. On a new chat the chat doesn't exist yet, so
there is no sandbox to put the file in, and the upload fails. That's true of every Open WebUI
version up to at least 0.12.3. Without the steps below, start the chat with a message first,
then attach files.

To make attaching before the first message work:

1. **Turn on holding in the orchestrator.** Add `STAGE_UPLOADS=true` to `.env` and run
   `docker compose up -d`. Files attached on a new chat are now kept in the orchestrator's data
   volume, under the user who uploaded them, until the chat's first message is sent.
2. **Install the "Terminal uploads" filter in Open WebUI.** Without it, held files are never
   delivered, so don't do step 1 alone.
   1. Go to **Admin Panel → Functions** and click **Create** (**+**).
   2. Replace the editor's contents with
      [`integrations/owui/filter_terminal_uploads.py`](../../integrations/owui/filter_terminal_uploads.py)
      and click **Save**.
   3. In the functions list, turn the filter's toggle on. Then open its **⋯** menu and choose
      **Global**, so it runs whatever model the chat uses.
   4. Click the filter's gear icon (**Valves**) and set **Admin Api Key** to `ORCH_ADMIN_API_KEY`
      from `.env`. Leave **Orchestrator Url** at `http://oto-orchestrator:8080` unless your
      orchestrator has another name. Save.
3. **Use filesystem uploads on the connection.** Under **Admin Settings → Integrations → Open
   Terminal**, edit the connection and set **Chat Uploads** to **Filesystem**.

How it works: when you send the first message, Open WebUI saves the chat with the attached
files' paths. The filter runs before the model, reads those paths from the saved chat, and
asks the orchestrator to put the held files into the chat's sandbox at exactly those paths. A
held file only ever goes to a chat of the user who uploaded it.

To check it: on a new chat, attach a file, send a message asking the model to list the home
folder, and look for the file. `docker exec oto-orchestrator oto status` shows
`held_uploads` (files still waiting), and the orchestrator log has a "Delivered held upload"
line for each delivered file.

Limits (all set in the orchestrator, see `ORCH_STAGING_*` in the
[main README](../../README.md#configuration)):
- Held files are deleted if no message is sent within **60 minutes** (for example, the file
  was attached and the chat was abandoned).
- At most **200 MB per file** and **1000 MB per user** can be held at once. Larger files can
  still be attached once the chat has started.
- Before the first message, files go to the sandbox's home folder (`/home/user`), and the file
  browser on the new chat stays empty until the chat starts.
- Temporary chats never get a chat ID, so files attached in them are never delivered.

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
