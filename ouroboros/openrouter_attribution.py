"""Canonical OpenRouter application attribution for Ouroboros traffic."""

from __future__ import annotations

OPENROUTER_APP_URL = "https://ouroboros-agent.ai/"
OPENROUTER_APP_TITLE = "Ouroboros"
OPENROUTER_APP_HEADERS = {
    "HTTP-Referer": OPENROUTER_APP_URL,
    "X-OpenRouter-Title": OPENROUTER_APP_TITLE,
}
