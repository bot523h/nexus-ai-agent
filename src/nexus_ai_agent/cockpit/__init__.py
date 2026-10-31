"""NEXUS Cockpit — an opt-in, effect-free window into the installed catalog.

Run ``python -m nexus_ai_agent.cockpit --help`` for deployment instructions.
This is NOT the Telegram runtime, a production-health dashboard, a command
executor, or an alternative capability authority. It uses the existing
``PackRuntime`` / ``CapabilityRegistry`` public introspection APIs. The only
POST validates an operation's *input model*: it never invokes its handler.

The independent entry point deliberately leaves the legacy API and the bot's
composition root untouched. No database, model, Telegram or cloud credentials
are needed. Production composition/authorization remains owned by the bot.
"""
