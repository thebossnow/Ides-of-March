#!/usr/bin/env python3
"""Alert on resolver failure — called by systemd OnFailure= hook."""
import os
import requests

token = os.environ.get("TELEGRAM_TOKEN", "")
chat = os.environ.get("TELEGRAM_CHAT_ID", "")
if token and chat:
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat,
            "text": "\u26a0\ufe0f weather-resolver FAILED \u2014 check journalctl -u weather-resolver",
        },
    )
