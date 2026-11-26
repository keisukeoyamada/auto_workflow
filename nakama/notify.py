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
