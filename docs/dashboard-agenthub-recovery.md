# Dashboard and AgentHub Recovery

## Hermes dashboard restart loop after Agent upgrades

### Failure signatures

- `hermes-dashboard` restarts continuously.
- Logs say a non-loopback dashboard bind requires an auth provider; `--insecure` no longer disables the gate.
- Plugin discovery may fail with missing registry methods such as `register_plugin_override_policy` when the process starts in `/workspace` and a workspace `tools` package shadows `/opt/hermes/tools`.
- Helm's TUI gateway falls back to `?token=` and receives WebSocket auth failures because gated dashboards accept cookie-minted, single-use `?ticket=` credentials instead.

### Durable stack contract

- Bind the dashboard to `0.0.0.0:9119` only with the bundled `basic` auth provider enabled.
- Keep the dashboard process working directory at `/opt/hermes`; set `TERMINAL_CWD=/workspace` so agent tools still execute in the workspace.
- Do not rely on `--insecure`; current Hermes releases ignore it for non-loopback binds.
- Store the password only in the ignored `config/dashboard-auth-password` file (`0600`). Store a scrypt hash and independent signing secret in ignored `config/dashboard-auth.env` (`0600`).
- `HELM_DASHBOARD_USERNAME`, `HELM_DASHBOARD_API_KEY`, and `HELM_DASHBOARD_WS_URL` must point Helm at the basic-auth flow and `ws://hermes-dashboard:9119/api/ws`.
- The dashboard healthcheck must require both `auth_required: true` and the `basic` provider. A public-but-unauthenticated dashboard must never report healthy.

### Operator login

Username: `josh`

Retrieve the password locally; do not paste it into chat or logs:

```bash
sudo cat /home/jmelchiori/docker-compose/hermes-webui-stack/config/dashboard-auth-password
```

### Verification

```bash
cd /home/jmelchiori/docker-compose/hermes-webui-stack

docker compose config >/dev/null
docker inspect hermes-webui-stack-dashboard \
  --format '{{.State.Status}}/{{.State.Health.Status}} restarts={{.RestartCount}}'

docker exec hermes-webui-stack-dashboard \
  /opt/hermes/.venv/bin/python3 -c \
  'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:9119/api/status")); assert d["auth_required"] and "basic" in d["auth_providers"]'
```

Then verify the Tailscale front door returns the same auth status at `/agent/api/status`, perform a basic-auth login, and mint `/agent/api/auth/ws-ticket`. Helm must also connect through `TuiGatewayClient` without falling back to `?token=`.

### Rollback

Before recreation, preserve the prior Compose file, dashboard token, and Helm SQLite database under ignored local paths. To roll back the tracked stack definition, restore the prior `docker-compose.yml`; restore the old local token only if the previous Helm client must be recovered. Recreate only `hermes-dashboard` and `helm`. Do not touch profile gateways or Postgres.

## AgentHub API unhealthy after Postgres restart

### Failure signature

- `/v1/agents` or another authenticated health probe returns `500`.
- Logs contain `psycopg.OperationalError: the connection is closed`.
- Postgres itself is healthy.

### Cause and recovery

Older AgentHub API processes retained a dead long-lived psycopg connection after Postgres restarted. Current AgentHub source constructs `PsycopgExecutor` with a connection factory and retries once on psycopg operational/interface errors.

Immediate recovery is a process restart only; do not recreate or modify the Postgres container:

```bash
docker restart hermes-webui-stack-agenthub-api
```

Verify with an authenticated API request and Docker health. For a controlled reconnect regression check, terminate only the AgentHub API's remote backend PID in `pg_stat_activity`, then issue one authenticated request. It must return `200`, create a new backend connection, and remain healthy. Never terminate the Postgres server or its local maintenance backend for this test.
