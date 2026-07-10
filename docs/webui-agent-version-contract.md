# Hermes WebUI Agent Version Contract

## Failure signature

Treat the WebUI and profile gateways as separate Hermes runtimes. A current WebUI release does not prove that the Hermes package inside `/app/venv` is current.

Typical skew symptoms:

- the WebUI settings page reports an old Hermes Agent version;
- `/api/plugins` or plugin visibility fails;
- basic chat fails before model execution with errors such as:
  - `ToolEntry` missing `dynamic_schema_overrides`;
  - `ToolRegistry` missing `register_plugin_override_policy`.

## Root cause

The upstream WebUI entrypoint installs Hermes into `/app/venv` from the first usable source tree in this order:

1. `/home/hermeswebui/.hermes/hermes-agent`
2. `/opt/hermes`
3. the `hermes-agent` package index release

The shared Hermes home in this stack can retain a legacy source checkout. Because it wins the source search order, a WebUI image update can reinstall that old checkout while the gateway images advance normally.

## Audit

```bash
cd /home/jmelchiori/docker-compose/hermes-webui-stack

docker exec hermes-webui-stack-webui \
  /app/venv/bin/python -c \
  'import importlib.metadata as m; print(m.version("hermes-agent"))'

docker exec hermes-webui-stack-gateway-infrastructure \
  /opt/hermes/.venv/bin/python3 -c \
  'import importlib.metadata as m; print(m.version("hermes-agent"))'

docker exec hermes-webui-stack-webui \
  /app/venv/bin/python -c \
  'import tools.registry as r; print(r.__file__); print(hasattr(r.ToolEntry,"dynamic_schema_overrides")); print(hasattr(r.ToolRegistry,"register_plugin_override_policy"))'
```

Do not use the WebUI `/health` endpoint alone: it can return 200 while the embedded Hermes runtime is incompatible with the WebUI API layer.

## Durable repair used by this stack

`Dockerfile.webui` copies the current official `nousresearch/hermes-agent:main` source into `/opt/hermes`. Build/runtime caches (`.venv`, `node_modules`, `.playwright`, `.git`) are excluded so the extra layer remains small.

The WebUI service mounts an empty read-only tmpfs over:

```text
/home/hermeswebui/.hermes/hermes-agent
```

This hides the legacy shared-home checkout without deleting it and forces the upstream entrypoint to install from `/opt/hermes`. The rollback source remains intact on the host.

`scripts/stack-update.sh` now compares the WebUI and infrastructure-gateway Hermes distribution versions after every update. Any mismatch is a hard failure, not a warning.

## Deployment

Recreating WebUI disconnects active browser chats. Get explicit approval, preserve the live image, then recreate only WebUI:

```bash
cd /home/jmelchiori/docker-compose/hermes-webui-stack

ts=$(date -u +%Y%m%dT%H%M%SZ)
old_id=$(docker inspect hermes-webui-stack-webui --format '{{.Image}}')
docker tag "$old_id" "local/hermes-webui-stack-webui:rollback-pre-agentfix-$ts"

docker compose build hermes-webui
docker compose up -d --no-deps --force-recreate hermes-webui
```

Expected blast radius: WebUI only, normally under 90 seconds. Profile gateways and messaging sessions remain up.

## Verification

1. Wait for `docker inspect ... .State.Health.Status` to become `healthy`.
2. Confirm WebUI and gateway Hermes versions are identical.
3. Confirm both compatibility attributes above are present.
4. Exercise the exact former crash path:

```bash
docker exec hermes-webui-stack-webui sh -lc \
  'cd /app && /app/venv/bin/python -c "from api.streaming import _install_streaming_cronjob_profile_wrapper as f; f(); print(\"chat_wrapper=OK\")"'
```

5. Create a disposable WebUI session, POST a minimal prompt to `/api/chat/start`, consume `/api/chat/stream`, require the expected assistant reply, and delete the session.
6. Scan fresh logs for `AttributeError`, `dynamic_schema_overrides`, `Failed to build plugin visibility`, and `stream error`.
7. Verify the external Tailscale/Caddy WebUI URL returns HTTP 200.

## Rollback

If verification fails, retag the preserved image and recreate only WebUI:

```bash
docker tag <rollback-image-tag> local/hermes-webui-stack-webui:latest
docker compose up -d --no-deps --force-recreate hermes-webui
```

Then verify health and capture the failed candidate logs before making another change.
