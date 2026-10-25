# integrations/

External-service adapters for NEXUS. Each subdirectory is a **thin boundary**:
it moves work across the process/network edge and returns a raw witness. No
adapter owns domain logic, authority, evidence or lineage — those stay in
`src/nexus_ai_agent/`.

| adapter | what it wraps | boundary doc |
|---|---|---|
| `trigger/` | Trigger.dev — background scheduling, retries, run lifecycle | [`trigger/README.md`](trigger/README.md) |
