# Hermes stack profile datadump: infrastructure + developer

This copy was live-verified and written onto the host via host-tools MCP.

## Live verification snapshot

Shared `hermes-webui-stack` services currently running:
- `hermes-gateway` (`hermes-webui-stack-gateway`): running / health=n/a
- `hermes-gateway-buyer` (`hermes-webui-stack-gateway-buyer`): running / health=n/a
- `hermes-gateway-developer` (`hermes-webui-stack-gateway-developer`): running / health=n/a
- `hermes-gateway-family` (`hermes-webui-stack-gateway-family`): running / health=n/a
- `hermes-gateway-financial` (`hermes-webui-stack-gateway-financial`): running / health=n/a
- `hermes-gateway-infrastructure` (`hermes-webui-stack-gateway-infrastructure`): running / health=n/a
- `hermes-gateway-personal` (`hermes-webui-stack-gateway-personal`): running / health=n/a
- `hermes-gateway-trading` (`hermes-webui-stack-gateway-trading`): running / health=n/a
- `openviking` (`hermes-webui-stack-openviking`): running / health=healthy
- `tailscale` (`hermes-webui-stack-tailscale`): running / health=healthy
- `hermes-webui` (`hermes-webui-stack-webui`): running / health=healthy

Legacy `hermes-ansilary` services currently running:
- `tailscale` (`hermes-ansilary-tailscale`): running / health=healthy
- `hermes-webui` (`hermes-webui`): running / health=healthy

## Live-confirmed profile facts

### Infrastructure
- API server port in profile `.env`: `8647`
- `WIKI_PATH`: `/workspace/llm-wikis/profiles/infrastructure`
- `llm-wiki` in `config.yaml`: yes
- Telegram token enabled in `.env`: no
- Discord token enabled in `.env`: no

`gh auth status` preview inside infrastructure gateway:
```text
github.com
  ✓ Logged in to github.com account jmelchiori (/opt/data/profiles/infrastructure/.config/gh/hosts.yml)
  - Active account: true
  - Git operations protocol: https
  - Token: gho_************************************
  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'
```

Generated override block:
```yaml
  hermes-gateway-infrastructure:
    build:
      context: .
      dockerfile: Dockerfile.gateway
    image: local/hermes-webui-stack-gateway:latest
    container_name: hermes-webui-stack-gateway-infrastructure
    user: "${WANTED_UID}:${WANTED_GID}"
    network_mode: service:tailscale
    depends_on:
      tailscale:
        condition: service_healthy
      openviking:
        condition: service_healthy
    environment:
      HERMES_HOME: /opt/data/profiles/infrastructure
      HERMES_CONFIG_PATH: /opt/data/profiles/infrastructure/config.yaml
      HOME: /opt/data/profiles/infrastructure
      XDG_STATE_HOME: /opt/data/profiles/infrastructure/.local/state
      GH_CONFIG_DIR: /opt/data/profiles/infrastructure/.config/gh
      API_SERVER_ENABLED: "true"
      API_SERVER_HOST: 0.0.0.0
      API_SERVER_PORT: "8647"
      OPENVIKING_ENDPOINT: http://127.0.0.1:1933
      TZ: ${TZ:-America/Chicago}
    working_dir: /workspace
    command: ["gateway", "run"]
    volumes:
      - ./hermes-home:/opt/data
      - ./workspace:/workspace
    restart: unless-stopped
```

### Developer
- API server port in profile `.env`: `8644`
- `WIKI_PATH`: `/workspace/llm-wikis/profiles/developer`
- `llm-wiki` in `config.yaml`: yes
- Telegram token enabled in `.env`: no
- Discord token enabled in `.env`: no

`gh auth status` preview inside developer gateway:
```text
github.com
  ✓ Logged in to github.com account jmelchiori (/opt/data/profiles/developer/.config/gh/hosts.yml)
  - Active account: true
  - Git operations protocol: https
  - Token: gho_************************************
  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'
```

Generated override block:
```yaml
  hermes-gateway-developer:
    build:
      context: .
      dockerfile: Dockerfile.gateway
    image: local/hermes-webui-stack-gateway:latest
    container_name: hermes-webui-stack-gateway-developer
    user: "${WANTED_UID}:${WANTED_GID}"
    network_mode: service:tailscale
    depends_on:
      tailscale:
        condition: service_healthy
      openviking:
        condition: service_healthy
    environment:
      HERMES_HOME: /opt/data/profiles/developer
      HERMES_CONFIG_PATH: /opt/data/profiles/developer/config.yaml
      HOME: /opt/data/profiles/developer
      XDG_STATE_HOME: /opt/data/profiles/developer/.local/state
      GH_CONFIG_DIR: /opt/data/profiles/developer/.config/gh
      API_SERVER_ENABLED: "true"
      API_SERVER_HOST: 0.0.0.0
      API_SERVER_PORT: "8644"
      OPENVIKING_ENDPOINT: http://127.0.0.1:1933
      TZ: ${TZ:-America/Chicago}
    working_dir: /workspace
    command: ["gateway", "run"]
    volumes:
      - ./hermes-home:/opt/data
      - ./workspace:/workspace
    restart: unless-stopped
```

## Full management datadump
# Hermes stack profile datadump: infrastructure + developer

## Scope and confidence

This is a management-oriented datadump for the `hermes-webui-stack` profiles `infrastructure` and `developer`, plus the related legacy `hermes-ansilary` stack.

Important caveat:
- this workspace does not have live visibility into `/home/jmelchiori/docker-compose/hermes-webui-stack`
- `docker` is not available here
- so this document is compiled from loaded stack skills and prior session recall, not a fresh host-side runtime inspection
- anything marked "confirmed in prior live inspection" came from earlier host-visible sessions
- anything marked "expected pattern" is the documented stack convention and should be verified on host before making changes

## 1. Big picture

Primary active stacks:
- newer shared-home stack: `~/docker-compose/hermes-webui-stack`
- legacy trimmed stack: `~/docker-compose/hermes-ansilary`

Core model for `hermes-webui-stack`:
- one shared `hermes-webui`
- one default/root `hermes-gateway`
- separate generated gateway containers for named profiles
- profiles are process-scoped via `HERMES_HOME` / `--profile`
- one gateway process equals one profile
- the WebUI profile switch does not make one shared gateway change brains dynamically

Operational consequence:
- background/API isolation is done with one gateway per profile
- profile management centers on generated gateway services, profile-local config/env, and profile-local persisted state

Known profile set from prior recall:
- `buyer`
- `developer`
- `family`
- `financial`
- `infrastructure`
- `personal`
- `trading`

## 2. Canonical paths and files

Shared stack root:
- `~/docker-compose/hermes-webui-stack`

Important stack files/scripts:
- `~/docker-compose/hermes-webui-stack/docker-compose.yml`
- `~/docker-compose/hermes-webui-stack/docker-compose.override.yml`
- `~/docker-compose/hermes-webui-stack/Dockerfile.webui`
- `~/docker-compose/hermes-webui-stack/Dockerfile.gateway`
- `~/docker-compose/hermes-webui-stack/scripts/bootstrap_profile_gateways.py`
- `~/docker-compose/hermes-webui-stack/scripts/generate_profile_gateway_compose.py`
- `~/docker-compose/hermes-webui-stack/scripts/reconcile_profile_gateways.py`
- `~/docker-compose/hermes-webui-stack/scripts/safe_update_stack.py`

Shared Hermes home:
- `~/docker-compose/hermes-webui-stack/hermes-home`

Named profile homes:
- `~/docker-compose/hermes-webui-stack/hermes-home/profiles/infrastructure`
- `~/docker-compose/hermes-webui-stack/hermes-home/profiles/developer`

Generated gateway compose output:
- `~/docker-compose/hermes-webui-stack/docker-compose.override.yml`

## 3. Profile gateway model you need to manage

Expected generated service pattern for any named profile:
- service name: `hermes-gateway-<profile>`
- container name: `hermes-webui-stack-gateway-<profile>`
- `network_mode: service:tailscale`
- mounts:
  - `./hermes-home:/opt/data`
  - `./workspace:/workspace`

Expected environment pattern for any named profile:
- `HERMES_HOME=/opt/data/profiles/<name>`
- `HERMES_CONFIG_PATH=/opt/data/profiles/<name>/config.yaml`
- `HOME=/opt/data/profiles/<name>`
- `XDG_STATE_HOME=/opt/data/profiles/<name>/.local/state`
- `GH_CONFIG_DIR=/opt/data/profiles/<name>/.config/gh`
- `API_SERVER_ENABLED=true`
- `API_SERVER_HOST=0.0.0.0`
- `OPENVIKING_ENDPOINT=http://127.0.0.1:1933`
- unique `API_SERVER_PORT`, assigned starting at `8643`

Profile gateway generation / reconciliation workflow:
1. discover profiles under `hermes-home/profiles/*`
2. ensure profile env exists
3. assign or preserve unique API port
4. comment inherited `TELEGRAM_*` and `DISCORD_*` unless profile explicitly opts in
5. do not copy Google OAuth artifacts by default
6. regenerate `docker-compose.override.yml`
7. apply changes

Standard apply command:
```bash
cd ~/docker-compose/hermes-webui-stack && python3 scripts/reconcile_profile_gateways.py --apply
```

## 4. Infrastructure profile datadump

### Identity and role
Expected runtime identity:
- profile name: `infrastructure`
- gateway container pattern: `hermes-webui-stack-gateway-infrastructure`
- profile home: `~/docker-compose/hermes-webui-stack/hermes-home/profiles/infrastructure`

### Confirmed in prior live inspection
From an earlier host-visible session, the following were confirmed for the `infrastructure` profile:
- profile config existed at:
  - `~/docker-compose/hermes-webui-stack/hermes-home/profiles/infrastructure/config.yaml`
- profile env existed at:
  - `~/docker-compose/hermes-webui-stack/hermes-home/profiles/infrastructure/.env`
- `llm-wiki` was explicitly present in the infrastructure profile skill list in `config.yaml`
- infrastructure `.env` contained:
  - `WIKI_PATH=/workspace/llm-wikis/profiles/infrastructure`
- profile-local skills existed under:
  - `.../profiles/infrastructure/skills/...`
- cron output/history existed under:
  - `.../profiles/infrastructure/cron/output/...`

### What that means operationally
The `infrastructure` profile appears to be more than a generic chat profile. It has evidence of:
- profile-local skills
- scheduled jobs / cron history
- explicit `llm-wiki` usage
- its own wiki storage path

That makes `infrastructure` the likely place to manage:
- operational notes and seeded technical knowledge
- stack runbooks / references meant to persist inside that profile
- any recurring automation jobs tied to infra management
- profile-specific agent behavior that should not bleed into other personas

### Infrastructure profile management checklist
When managing `infrastructure`, the important places to inspect first are:
- `~/docker-compose/hermes-webui-stack/hermes-home/profiles/infrastructure/config.yaml`
- `~/docker-compose/hermes-webui-stack/hermes-home/profiles/infrastructure/.env`
- `~/docker-compose/hermes-webui-stack/hermes-home/profiles/infrastructure/.config/gh/hosts.yml`
- `~/docker-compose/hermes-webui-stack/hermes-home/profiles/infrastructure/skills/`
- `~/docker-compose/hermes-webui-stack/hermes-home/profiles/infrastructure/cron/`
- `/workspace/llm-wikis/profiles/infrastructure`
- generated service entry in `docker-compose.override.yml`

Questions to answer before changing it:
- what API port is assigned to `infrastructure` in the generated override?
- does the profile intentionally own any Telegram/Discord creds, or are they still bootstrap-commented?
- is `llm-wiki` still enabled and pointing at the intended path?
- are there profile-local skills that have diverged from shared skills?
- are there cron jobs that depend on current path/env assumptions?

### Useful verification commands for infrastructure
Profile service presence:
```bash
cd ~/docker-compose/hermes-webui-stack && docker compose ps
```

Inspect profile from its gateway:
```bash
docker exec hermes-webui-stack-gateway-infrastructure sh -lc '/opt/hermes/.venv/bin/python -m hermes_cli.main profile show infrastructure'
```

Inspect runtime env basics:
```bash
docker exec hermes-webui-stack-gateway-infrastructure sh -lc 'env | egrep "HERMES_HOME|HERMES_CONFIG_PATH|HOME|GH_CONFIG_DIR|API_SERVER_PORT|WIKI_PATH"'
```

Open a chat in that profile:
```bash
docker exec -it hermes-webui-stack-gateway-infrastructure sh -lc '/opt/hermes/.venv/bin/python -m hermes_cli.main chat'
```

## 5. Developer profile datadump

### Identity and role
Expected runtime identity:
- profile name: `developer`
- gateway container: `hermes-webui-stack-gateway-developer`
- profile home: `~/docker-compose/hermes-webui-stack/hermes-home/profiles/developer`

### Confirmed in prior recall / usage guidance
From prior sessions, the following were established for `developer`:
- the stack uses a dedicated developer gateway container:
  - `hermes-webui-stack-gateway-developer`
- a safe stack-specific way to get a Hermes chat session on that profile is:
```bash
cd ~/docker-compose/hermes-webui-stack && docker exec -it hermes-webui-stack-gateway-developer sh -lc '/opt/hermes/.venv/bin/python -m hermes_cli.main chat'
```
- a profile inspection command used in prior guidance was:
```bash
cd ~/docker-compose/hermes-webui-stack && docker exec hermes-webui-stack-gateway-developer sh -lc '/opt/hermes/.venv/bin/python -m hermes_cli.main profile show developer'
```

### What is not yet confirmed from recall
Compared with `infrastructure`, I do not have a prior live-inspection recall of a developer-specific `.env` or unique feature like `WIKI_PATH`.

So for `developer`, these are documented expectations rather than confirmed current facts:
- profile-local home exists under `hermes-home/profiles/developer`
- generated gateway service exists in `docker-compose.override.yml`
- profile-local `GH_CONFIG_DIR` is `/opt/data/profiles/developer/.config/gh`
- unique API port exists for the profile
- inherited chat/messaging creds are commented unless explicitly enabled

### Developer profile management checklist
Inspect first:
- `~/docker-compose/hermes-webui-stack/hermes-home/profiles/developer/config.yaml`
- `~/docker-compose/hermes-webui-stack/hermes-home/profiles/developer/.env`
- `~/docker-compose/hermes-webui-stack/hermes-home/profiles/developer/.config/gh/hosts.yml`
- `~/docker-compose/hermes-webui-stack/hermes-home/profiles/developer/skills/`
- generated developer service in `docker-compose.override.yml`

Primary management questions:
- what exact API port is assigned to developer?
- does developer have any extra coding/tooling env not present in other profiles?
- are GitHub credentials present and readable via the profile-local `GH_CONFIG_DIR`?
- does developer intentionally own any messaging or webhook credentials?
- is the developer profile using shared skills only, or profile-local overrides too?

### Useful verification commands for developer
Profile service presence:
```bash
cd ~/docker-compose/hermes-webui-stack && docker compose ps
```

Inspect profile from its gateway:
```bash
docker exec hermes-webui-stack-gateway-developer sh -lc '/opt/hermes/.venv/bin/python -m hermes_cli.main profile show developer'
```

Inspect runtime env basics:
```bash
docker exec hermes-webui-stack-gateway-developer sh -lc 'env | egrep "HERMES_HOME|HERMES_CONFIG_PATH|HOME|GH_CONFIG_DIR|API_SERVER_PORT"'
```

Open a chat in that profile:
```bash
docker exec -it hermes-webui-stack-gateway-developer sh -lc '/opt/hermes/.venv/bin/python -m hermes_cli.main chat'
```

## 6. GitHub auth and credential persistence

Important established pattern:
- GitHub auth should persist in bind-mounted profile-local config dirs
- default gateway uses:
  - `GH_CONFIG_DIR=/opt/data/.config/gh`
- named profiles use:
  - `GH_CONFIG_DIR=/opt/data/profiles/<name>/.config/gh`

Host is the canonical GitHub identity source:
- host file of record:
  - `~/.config/gh/hosts.yml`
- updater flows were adjusted to sync the host `hosts.yml` into stack-local bind-mounted targets before recreate

Known synced targets from prior recall included:
- `~/docker-compose/hermes-webui-stack/hermes-home/.config/gh/hosts.yml`
- `~/docker-compose/hermes-webui-stack/hermes-home/profiles/developer/.config/gh/hosts.yml`
- `~/docker-compose/hermes-webui-stack/hermes-home/profiles/infrastructure/.config/gh/hosts.yml`
- other profile-local `hosts.yml` files as well

Management implication:
- if `gh auth status` fails inside a profile gateway after recreate, inspect the profile-local `GH_CONFIG_DIR` first
- do not assume auth belongs under an unmounted default `$HOME/.config/gh`

Verification examples:
```bash
docker exec hermes-webui-stack-gateway-developer gh auth status
```

```bash
docker exec hermes-webui-stack-gateway-infrastructure gh auth status
```

## 7. Update and recreate rules that matter

### Safe update tooling
For `hermes-webui-stack`, the operational updater is expected to be:
- `~/docker-compose/hermes-webui-stack/scripts/safe_update_stack.py`

For generated profile gateways, remember:
- `docker-compose.override.yml` is part of the active runtime set
- if the gateway image is rebuilt, verify all containers using that tag, not just the default `hermes-gateway`

### Critical Tailscale sidecar rule
If `tailscale` is recreated, every service using `network_mode: service:tailscale` must also be recreated.

Treat this as one recreation set:
- `tailscale`
- `hermes-webui`
- `obsidian`
- `openviking`
- all `hermes-gateway*` services

Reason:
- otherwise services can look healthy while still attached to the old network namespace
- symptom can be Tailscale Serve returning `502` with backend connection refused errors

### Controller safety rule
If you are operating from inside the very WebUI/container session you are managing:
- do not restart the container/service hosting your active controller until you have a host-side control path
- otherwise you can kill the session doing the maintenance

## 8. Legacy ansilary stack datadump

### Identity and role
Legacy stack path:
- `~/docker-compose/hermes-ansilary`

Current intended architecture:
- active:
  - `tailscale`
  - `hermes-webui`
- retired/offline but preserved on disk:
  - `hermes-studio`
  - `mimir`
  - `redis`

Important rule:
- do not casually re-add retired services as routine maintenance
- treat reintroduction as an architecture change

### Exposure model
The legacy stack uses a dedicated Tailscale sidecar.

Expected URL:
- `https://hermes-ansilary.tail1b7609.ts.net/`

Serve route:
- `/` -> `http://127.0.0.1:8787`

Host-level `tailscale serve` on `josh-m800` should remain disabled for this stack.
Expected host result:
- `tailscale serve status` -> `No serve config`

### Important files
- `~/docker-compose/hermes-ansilary/docker-compose.yml`
- `~/docker-compose/hermes-ansilary/.env`
- `~/docker-compose/hermes-ansilary/config/serve-config.json`
- `~/docker-compose/hermes-ansilary/tailscale-state/`
- `~/docker-compose/hermes-ansilary/scripts/safe_update_ancillary.py`
- `~/docker-compose/hermes-ansilary/verify-hermes-webui.sh`

### Known current state from prior operational recall
The ansilary stack is intentionally trimmed:
- only `hermes-webui` remains active on `8787`
- `hermes-studio` is retired
- `mimir` / `personal-ai-portal` is preserved on disk but offline
- host Hermes still separately uses gateway `8642` and dashboard `9119`

### Updater behavior and caveat
Canonical updater:
- `~/docker-compose/hermes-ansilary/scripts/safe_update_ancillary.py`

The updater should:
- sync host GitHub auth
- try to update the host `~/.hermes/hermes-agent` checkout
- recreate/verify legacy `hermes-webui`
- verify sidecar serve and health
- report retired components as intentionally offline

Known caveat from prior runs:
- updater can report `partial_failure` even when the live stack is healthy
- one known cause is unresolved git conflict in `~/.hermes/hermes-agent` such as `uv.lock`
- another is a startup race where early health probes hit `connection refused` or external `502` before `hermes-webui` is fully ready

### Verification commands for ansilary
```bash
cd ~/docker-compose/hermes-ansilary && docker compose ps
```

```bash
cd ~/docker-compose/hermes-ansilary && ./verify-hermes-webui.sh
```

```bash
docker exec hermes-ansilary-tailscale tailscale serve status
```

```bash
docker exec hermes-ansilary-tailscale wget -qO- http://127.0.0.1:8787/health
```

```bash
curl -fsSL https://hermes-ansilary.tail1b7609.ts.net/health
```

## 9. Fast operator playbook

If someone needs to manage these stacks quickly, this is the shortest useful sequence.

### For infrastructure profile
1. `cd ~/docker-compose/hermes-webui-stack && docker compose ps`
2. inspect generated service entry in `docker-compose.override.yml`
3. inspect `profiles/infrastructure/config.yaml` and `.env`
4. check `WIKI_PATH` and `llm-wiki` config
5. run `profile show infrastructure`
6. check `gh auth status`
7. if profile list/env changed, run `python3 scripts/reconcile_profile_gateways.py --apply`

### For developer profile
1. `cd ~/docker-compose/hermes-webui-stack && docker compose ps`
2. inspect generated developer service entry in `docker-compose.override.yml`
3. inspect `profiles/developer/config.yaml` and `.env`
4. run `profile show developer`
5. open a chat in `hermes-webui-stack-gateway-developer`
6. check `gh auth status`
7. if profile/env/gateway defs changed, run `python3 scripts/reconcile_profile_gateways.py --apply`

### For ansilary stack
1. `cd ~/docker-compose/hermes-ansilary && docker compose ps`
2. run `./verify-hermes-webui.sh`
3. verify Tailscale Serve and `/health`
4. if updater reports `partial_failure`, distinguish service health from host checkout maintenance failure

## 10. Gaps to verify live on host

These are the most important things I would still verify live on host before treating this as a full source of truth:
- exact assigned API ports for `infrastructure` and `developer`
- current container presence/health for `hermes-webui-stack-gateway-infrastructure` and `hermes-webui-stack-gateway-developer`
- exact current contents of each profile's `.env` and `config.yaml`
- whether developer also has a profile-local wiki path or other special tooling env
- whether the host `~/.hermes/hermes-agent` checkout conflict has been resolved for ansilary updater health
