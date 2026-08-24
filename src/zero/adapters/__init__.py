"""External transport adapters.

Adapters depend on the canonical domain event envelope and an injected
transport.  They do not own project state or credentials.
"""

from .discord import DiscordAdapter
from .messaging import RetryPolicy, safe_render_text
from .telegram import TelegramAdapter

__all__ = ["DiscordAdapter", "RetryPolicy", "TelegramAdapter", "safe_render_text"]
