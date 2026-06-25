# Profile-aware gateway plan for hermes-webui-stack

> For Hermes: use subagent-driven-development if implementing this later.

Goal: make the shared-home `hermes-webui-stack` handle Hermes profiles intentionally instead of silently running only the default profile in the gateway.

Current findings
- The live gateway container runs with `HERMES_HOME=/opt/data`, which is the shared root `~/docker-compose/hermes-webui-stack/hermes-home`.
- Hermes profiles are process-scoped via `HERMES_HOME` and `--profile`; they are not a per-message toggle inside one running gateway process.
- Hermes docs describe profiles as separate isolated environments with their own config, sessions, memories, skills, cron jobs, state DB, and gateway PID.
- Hermes docs also describe one gateway per profile as the intended model, with separate bot tokens or separate API server instances.
- In this stack, `family` exists at `~/docker-compose/hermes-webui-stack/hermes-home/profiles/family`, but the live gateway is only running the default profile.
- `active_profile` at the shared root is blank, so the default profile is active.
- The `family` profile currently lacks copied OAuth/cron artifacts such as `google_token.json` and `cron/jobs.json`, so even if a family gateway were started today it would not inherit those automatically.
- Hermes supports proxy mode: a gateway can handle platform I/O and forward agent work to a remote/profile-specific API server via `GATEWAY_PROXY_URL` and `GATEWAY_PROXY_KEY`.

What this means
- Profiles are not fully accounted for in the current gateway setup.
- The current containerized gateway is effectively a default-profile gateway only.
- Changing WebUI profile in the browser does not automatically make the background gateway process switch profile.
- A single gateway process cannot safely act as multiple Hermes profiles at once unless Hermes itself adds first-class per-message profile routing.

Recommended architecture
- Keep the existing shared-home WebUI container as-is.
- Treat each Hermes profile as its own runtime unit for background services.
- Use one long-running gateway/API process per profile.
- Make profile processes automatic from the stack by generating one compose service per profile.

Recommended implementation shape
1. Default profile service
   - Keep `hermes-gateway` for the default profile.
   - `HERMES_HOME=/opt/data`
   - mounts `./hermes-home:/opt/data`
   - API server on 8642

2. Named profile services
   - Add one service per named profile, for example `hermes-gateway-family`.
   - Run with either:
     - `HERMES_HOME=/opt/data/profiles/family`, or
     - command `hermes --profile family gateway run`
   - Prefer explicit `HERMES_HOME=/opt/data/profiles/family` in Docker so all path resolution is unambiguous.
   - Give each profile API server a unique localhost port, for example:
     - default -> 8642
     - family -> 8643
     - future profiles increment upward

3. Profile bootstrap sync
   - Create a host-side script that discovers `hermes-home/profiles/*`.
   - For each profile, ensure only explicitly approved shared credentials/files are copied or templated where appropriate.
   - For this stack right now:
     - allow `auth.json` only when needed
     - do NOT copy Google OAuth artifacts by default
     - do NOT seed Gmail cron jobs by default
   - Do not blindly copy everything; support an allowlist so profile-specific isolation is preserved.

4. Automatic compose generation
   - Generate a small compose override file such as `docker-compose.profiles.yml` from discovered profiles.
   - The generator should emit one gateway service per profile with:
     - shared sidecar network namespace
     - shared stack workspace mount
     - profile-specific `HERMES_HOME`
     - unique API server port
     - optional per-profile env file if needed later
   - Re-run the generator whenever profiles are added/removed.

5. Optional routing layer for messaging
   - If the goal is simultaneous messaging identities, each profile needs its own bot token and its own gateway service.
   - If the goal is one messaging identity but different agent brains by channel, add a routing layer outside Hermes:
     - one ingress gateway in proxy mode
     - route selected channels/threads to profile-specific API servers
   - This is feasible but is custom stack logic, not Hermes’ default profile model.

6. WebUI alignment
   - WebUI already sees profiles from the shared home.
   - Add a small stack note/UI runbook stating:
     - WebUI profile selection affects chat sessions in the browser.
     - Background gateways remain separate per profile.
     - Messaging/API behavior follows the specific gateway service for that profile, not the currently selected browser profile.

Suggested rollout tasks
1. Inventory named profiles and decide which need background gateways.
2. Add a profile bootstrap script under `~/docker-compose/hermes-webui-stack/scripts/`.
3. Add a compose-profile generator under `~/docker-compose/hermes-webui-stack/scripts/`.
4. Generate `docker-compose.profiles.yml`.
5. Bring up one named profile gateway first (`family`) on a new API port.
6. Verify `family` gateway state, API health, and any profile-specific cron jobs.
7. Update README and stack skills to explain the per-profile gateway model.
8. Optionally add a cron job or file-watch wrapper to regenerate/redeploy profile services when profiles change.

Verification checklist
- `docker compose ps` shows one gateway service per intended profile.
- Each service reports a distinct `HERMES_HOME`.
- Each profile API server responds on its assigned port.
- `hermes profile list` and service inventory agree.
- No token-lock conflicts appear across profiles.
- WebUI profile switching still works.
- Cron jobs appear in the expected profile home only.

Decision point
- If you want the Hermes-native model, implement one gateway per profile.
- If you want one Discord/Telegram identity to automatically switch brains by channel, that needs custom proxy routing on top of Hermes, not just turning on profiles.
