"""Optional Slack Incoming Webhook notifications (the 'Nakama channel')."""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


def post_to_slack(webhook_env: str | None, text: str) -> bool:
    """Post text to Slack if the webhook env var is set. Never raises."""
    url = os.environ.get(webhook_env or "", "")
    if not url:
        return False
    body = json.dumps({"text": text[:39000]}).encode("utf-8")  # Slack message limit ~40k chars
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError) as exc:
        log.warning("Slack notification failed: %s", exc)
        return False


def post_to_teams(webhook_env: str | None, text: str, title: str = "Nakama") -> bool:
    """Post to a Teams chat/channel through a Teams "Workflows" webhook (Power Automate). Never raises.

    Create it in Teams: Workflows > "Post to a chat when a webhook request is received" (or the channel
    variant), copy the URL into .env and name that variable in fleet.yaml notify.teams_webhook_env.
    """
    url = os.environ.get(webhook_env or "", "")
    if not url:
        return False
    card = {
        "type": "AdaptiveCard", "version": "1.4", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [{"type": "TextBlock", "text": title[:200], "weight": "Bolder", "wrap": True},
                 {"type": "TextBlock", "text": text[:20000], "wrap": True}],
    }
    body = json.dumps({"type": "message", "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError) as exc:
        log.warning("Teams notification failed: %s", exc)
        return False


def notify_all(notify_cfg: dict | None, text: str, title: str = "Nakama") -> dict[str, bool]:
    """Send to every configured channel (Slack and/or Teams)."""
    cfg = notify_cfg or {}
    return {"slack": post_to_slack(cfg.get("slack_webhook_env"), f"*{title}*\n{text}"),
            "teams": post_to_teams(cfg.get("teams_webhook_env"), text, title)}
