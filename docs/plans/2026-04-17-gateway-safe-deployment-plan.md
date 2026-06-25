# Obsolete plan: isolated gateway deployment

Status: retained only as historical context.

This document described the temporary isolated `gateway-home` / `gateway-workspace` architecture that was used before the stack was migrated back to the upstream shared-home two-container model.

Current architecture:
- `hermes-webui` and `hermes-gateway` both use `~/docker-compose/hermes-webui-stack/hermes-home`
- both services use `~/docker-compose/hermes-webui-stack/workspace`
- `gateway-home` and `gateway-workspace` are not the active runtime anymore

Reason this plan is obsolete:
- it diverged from upstream guidance for hermes-webui
- it introduced split-brain config/state behavior between WebUI and gateway
- the real root cause of the earlier outage was UID mismatch on the shared bind mount, not the shared-home model itself

If a future task needs the current architecture, use these instead:
- `~/docker-compose/hermes-webui-stack/README.md`
- `~/docker-compose/hermes-webui-stack/hermes-home/skills/devops/hermes-webui-stack-quick-ops/SKILL.md`
- `~/docker-compose/hermes-webui-stack/hermes-home/skills/devops/hermes-webui-stack-management/SKILL.md`
