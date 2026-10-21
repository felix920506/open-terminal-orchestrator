# Add terminals to an existing Open WebUI

This adds per-chat terminal sandboxes to an Open WebUI you already run with Docker Compose.
Your existing data, users and settings are kept. Tested with Open WebUI 0.11.1, 0.11.4 and 0.12.2.

Starting from scratch instead? Use [`examples/open-webui`](../open-webui), which needs no setup
in the UI.

## 1. Add the services

Copy [`docker-compose.terminals.yml`](docker-compose.terminals.yml) next to your
`docker-compose.yml`. It adds two services (`orchestrator`, `socket-proxy`) and puts your
Open WebUI service on one extra network. It changes nothing else in your file.

If your Open WebUI service has a different name than `open-webui`, rename it in two places in
the copied file: the key under `services:` and the `ORCH_ALLOWED_CLIENTS` entry.

<details>
<summary>Prefer to edit your own docker-compose.yml? Here is exactly what to add.</summary>

Add this network to your Open WebUI service. Keep any networks it already lists. If it lists
none, it is on `default`, so keep that.

```yaml
services:
  open-webui:
    # ...your existing settings...
    networks: [default, terminals]
```

Then append the `orchestrator` and `socket-proxy` services, the `orchestrator-data` volume and
the `terminals` and `control` networks from
[`docker-compose.terminals.yml`](docker-compose.terminals.yml) to the matching top-level
sections of your file.
</details>

## 2. Add the keys

Append three random keys to the `.env` file next to your compose file:

```bash
for v in ORCH_API_KEY ORCH_ADMIN_API_KEY ORCH_SECRET_KEY; do echo "$v=$(openssl rand -hex 32)"; done >> .env
echo "COMPOSE_FILE=docker-compose.yml:docker-compose.terminals.yml" >> .env   # optional, see below
```

With `COMPOSE_FILE` set, plain `docker compose` commands include the terminal services.
Without it, pass both files every time:
`docker compose -f docker-compose.yml -f docker-compose.terminals.yml …`.

Optional settings for `.env`:

| Variable | Default | |
|---|---|---|
| `SANDBOX_IMAGE` | `ghcr.io/open-webui/open-terminal:latest` | `ghcr.io/felix920506/open-terminal-scientific:latest` adds scientific Python, image processing and Excel, PDF, Word and PowerPoint handling ([contents](../../README.md#sandbox-images)) |
| `MAX_RUNNING_SANDBOXES` | `20` | Sandboxes running at once |
| `OTO_VERSION` | `0.1.4` | Orchestrator image tag |

To change `SANDBOX_IMAGE` later, see [changing the sandbox image](../open-webui/README.md#changing-the-sandbox-image).

## 3. Start

```bash
docker compose up -d
docker exec oto-orchestrator oto status    # policy "default" should become "ready"
```

Open WebUI is recreated once to join the new network, so it is unavailable for about a
minute. Its data volume is untouched.
On first start the orchestrator pulls the sandbox image in the background; the full image is
about 6 GB.

## 4. Connect Open WebUI to the orchestrator

Open WebUI keeps its settings in its database, so an existing install has to be told about the
terminal once. (The `TERMINAL_SERVER_CONNECTIONS` variable used by the fresh-install example is
read only when Open WebUI starts with an empty database.)

> [!WARNING]
> **There are three places in Open WebUI that accept this URL, and only one is right.** Use
> **Admin Settings → Integrations → Open Terminal**.
>
> | Place | What it creates | Works? |
> |---|---|---|
> | **Admin Settings** → Integrations → **Open Terminal** | A **System** terminal: Open WebUI's server talks to the orchestrator and sends the chat ID | **Yes** |
> | **Admin Settings** → Integrations → **External Tool Servers** | A generic OpenAPI tool server, with no idea of chats | No |
> | Your personal **Settings** (profile menu) → Integrations → **Open Terminal** | A **Direct** terminal: your browser calls the URL itself | No. The browser can't reach `oto-orchestrator`, and no chat ID is sent |
>
> The admin and personal pages look almost identical. Check that you are under **Admin
> Settings** before adding the connection.

1. Under **Open Terminal**, click **+** and fill in:

   | Field | Value |
   |---|---|
   | URL | `http://oto-orchestrator:8080` |
   | API Key | the `ORCH_API_KEY` value from `.env` |

2. **Click the verify button** (the icon at the end of the URL field, tooltip "Verify
   Connection"). It should say **Connected (Orchestrator)**. You must do this: the
   **Orchestrator** section in the next step only appears after a successful verify, and it
   starts collapsed.
3. Expand **Orchestrator** and set:

   | Field | Value |
   |---|---|
   | Terminal Contexts → Chat | **Per chat**. The default, **Shared**, does not work with this orchestrator: every chat request fails with HTTP 409. |
   | Policy ID | `default` |

   The policy's image, CPU, memory, storage and idle timeout load below it. Setting a
   different image here overrides `SANDBOX_IMAGE` from then on.
4. By default only admins can use the connection. Use the access button to grant it to users
   or groups, then **Save**.

To check an existing connection, edit it, click verify, and expand **Orchestrator**: Chat must
say **Per chat**.

In a chat, the terminal menu lists admin connections under **System** and personal ones under
**Direct**. Always pick the one under **System**. If the orchestrator also shows up under
**Direct**, someone added it in their personal settings: delete it in their **Settings →
Integrations → Open Terminal**. To stop users from adding their own, turn off **Direct Tool
Servers** under **Admin Panel → Users → Groups → Default permissions**.

<details>
<summary>Or register it from the command line</summary>

This needs an admin API key (**Settings → Account → API keys**; an admin may have to enable API
keys under **Admin Settings → General** first), plus `curl` and `jq`. It keeps any terminal
connections you already have and gives every user access.

```bash
OWUI=http://localhost:3000 TOKEN=sk-your-admin-api-key
ORCH_API_KEY=$(sed -n 's/^ORCH_API_KEY=//p' .env)
curl -fsS "$OWUI/api/v1/configs/terminal_servers" -H "Authorization: Bearer $TOKEN" |
jq --arg key "$ORCH_API_KEY" '.TERMINAL_SERVER_CONNECTIONS |= (map(select(.id != "oto")) + [{
  id: "oto", name: "Terminal", enabled: true,
  url: "http://oto-orchestrator:8080", path: "/openapi.json", key: $key, auth_type: "bearer",
  server_type: "orchestrator", policy_id: "default",
  config: {contexts: {chat: {context_id: "chat_id"}},
           access_grants: [{principal_type: "user", principal_id: "*", permission: "read"}]}}])' |
curl -fsS "$OWUI/api/v1/configs/terminal_servers" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d @- >/dev/null && echo registered
```
</details>

## 5. Use it

In a chat, pick **Terminal** from the **System** group of the terminal menu in the input bar
(not **Direct**; see the warning in step 4). The model can now run commands, and the file
browser shows that chat's sandbox. To have the terminal selected by default, edit a model in
**Admin Settings → Models** and set **Default terminal**.

The rest works as in the fresh-install example:
- [what users get](../open-webui/README.md#what-users-get)
- [the optional cleanup and reset functions](../open-webui/README.md#optional-chat-cleanup-and-a-reset-button)
- [administration](../open-webui/README.md#administration)

## Undo

```bash
# 1. Delete the sandboxes (the orchestrator creates them outside Compose)
docker exec oto-orchestrator sh -c 'oto ls --json | python -c "import json,sys; [print(b[\"id\"]) for b in json.load(sys.stdin)]" | xargs -rn1 oto destroy'
```

2. Remove `COMPOSE_FILE` and the `ORCH_*` lines from `.env`, and delete
   `docker-compose.terminals.yml`.
3. Remove the leftover containers, networks and volume:

```bash
docker compose up -d --remove-orphans   # removes the two services and detaches Open WebUI from the network
p=$(docker compose config --format json | jq -r .name)   # your Compose project name
docker network rm "${p}_terminals" "${p}_control" oto-egress
docker volume rm "${p}_orchestrator-data"
```

4. Delete the connection under **Admin Settings → Integrations → Open Terminal**.

## Open WebUI not in Compose?

Run the orchestrator on its own with [`deploy/docker-compose.yml`](../../deploy/docker-compose.yml),
using `OTO_IMAGE=ghcr.io/felix920506/open-terminal-orchestrator:0.1.4` and `--no-build`. Then
connect as in step 4.
- **Open WebUI started with `docker run` on the same host:** set `OTO_WEBUI_NETWORK` to a
  Docker network Open WebUI is attached to, and `ORCH_ALLOWED_CLIENTS` to its container name.
- **Open WebUI on another host or in Kubernetes:** publish the orchestrator with `OTO_PORT`
  (for example `0.0.0.0:8080`). Set `ORCH_ALLOWED_CLIENTS` to the address Open WebUI connects
  from, and use `http://<docker-host>:8080` as the URL in step 4. The orchestrator must run on
  the Docker host that will hold the sandboxes.
