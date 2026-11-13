"""Уведомления в Telegram."""
from __future__ import annotations

import logging
from typing import Optional

import httpx

log = logging.getLogger("printhub.notify")


async def send_telegram(token: str, chat_id: str, text: str, photo: Optional[bytes] = None) -> bool:
    if not token or not chat_id:
        return False
    base = f"https://api.telegram.org/bot{token}"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            if photo:
                r = await c.post(f"{base}/sendPhoto", data={"chat_id": chat_id, "caption": text[:1000],
                                                             "parse_mode": "HTML"},
                                 files={"photo": ("snapshot.jpg", photo, "image/jpeg")})
            else:
                r = await c.post(f"{base}/sendMessage", data={"chat_id": chat_id, "text": text,
                                                               "parse_mode": "HTML"})
            if r.status_code != 200:
                log.warning("Telegram: %s %s", r.status_code, r.text[:200])
                return False
            return True
    except httpx.HTTPError as e:
        log.warning("Telegram недоступен: %s", e)
        return False
