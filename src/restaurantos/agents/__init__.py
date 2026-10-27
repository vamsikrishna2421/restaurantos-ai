"""Model-backed agents (ARCHITECTURE §2 and §5): the Order Agent (P13).

- ``agents.order_prompt``: prompt ``o1`` (cached system prefix, user message, strict schema, cache key);
- ``agents.order_agent``: ``OrderAgent.parse`` / ``warm`` and the ``agents.order_agent`` provider;
- ``agents.reply_filter``: when the model's own clarification may be spoken (``allowed``, ``speakable``);
- ``agents.keywords``: the ≤100 Voice Transcribe keywords of a menu.

Nothing is imported here, so importing one module never pulls in the others.
"""
