# Research Tools — Agent 4 (Independent, No Project Dependencies)

This folder is reserved for **independent research scripts** that must not modify the project's source, dependencies, or branches.

Per mission rule: **NO CODE OWNERSHIP — NO IMPLEMENTATION FIRST.**

- Any script here must be **standalone** (`python research/tools/<script>.py`), not imported by `src/`.
- Do not add entries to `pyproject.toml` / `requirements` / `package.json` of the main project.
- Do not import `nexus_ai_agent.*` or touch `bot/handlers.py`, FeatureEngines, etc.
- Keep artifacts out of Git (large datasets → external storage per repo conventions).

No scripts were required for stages 1–10; all findings cite primary + independent sources with evidence tags. This README preserves the tool boundary and prevents accidental pollution.

If a future research automation is added (e.g., source freshness checker):

```bash
python research/tools/check_sources.py --manifest research/sources.json
```

Keep it hermetic and read-only.

