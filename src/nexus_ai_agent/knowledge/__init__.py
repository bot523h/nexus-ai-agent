"""Knowledge system: Wikipedia, web search, and unified knowledge manager.

Every retrieval path in this package reports failure through the typed
``ExternalSourceError`` contract in ``nexus_ai_agent.integrations.external``
and attaches ``Provenance`` to whatever it returns.
"""
