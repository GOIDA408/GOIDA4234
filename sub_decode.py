"""Расшифровка base64-подписок (subscription links)."""
from __future__ import annotations

import base64
import binascii
import json
import re

_PROXY_MARKERS = ("vless://", "vmess://", "trojan://", "ss://", "hysteria2://", "hy2://")


def _looks_like_proxy_list(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _PROXY_MARKERS)


def b64_decode_any(text: str) -> str:
    t = re.sub(r"\s+", "", text.strip())
    if len(t) < 8:
        return ""
    variants = [t, t.replace("-", "+").replace("_", "/")]
    for variant in variants:
        padded = variant + "=" * ((4 - len(variant) % 4) % 4)
        try:
            raw = base64.b64decode(padded, validate=False)
            return raw.decode("utf-8", errors="ignore")
        except (ValueError, binascii.Error):
            continue
    return ""


def unwrap_subscription(text: str, *, max_depth: int = 3) -> str:
    """
    Развернуть содержимое подписки:
    - plain text (vless:// построчно)
    - base64 (standard / url-safe)
    - несколько слоёв base64
    - JSON с полем data/content/subscription
    """
    if not text or not text.strip():
        return ""

    queue: list[str] = [text.strip()]
    seen: set[str] = set()
    out: list[str] = []
    depth = max_depth

    while queue and depth >= 0:
        chunk = queue.pop(0)
        if chunk in seen:
            continue
        seen.add(chunk)

        if _looks_like_proxy_list(chunk):
            out.append(chunk)
            continue

        decoded = b64_decode_any(chunk)
        if decoded and decoded not in seen:
            queue.append(decoded)
            depth -= 1
            continue

        stripped = chunk.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                pass
            else:
                blobs: list[str] = []
                if isinstance(data, dict):
                    for key in ("data", "content", "subscription", "body", "result", "nodes"):
                        val = data.get(key)
                        if isinstance(val, str) and val.strip():
                            blobs.append(val)
                    for key in ("links", "nodes", "proxies"):
                        val = data.get(key)
                        if isinstance(val, list):
                            blobs.append("\n".join(str(x) for x in val))
                elif isinstance(data, list):
                    blobs.append("\n".join(str(x) for x in data))
                for blob in blobs:
                    if blob not in seen:
                        queue.append(blob)
                depth -= 1
                continue

        out.append(chunk)

    return "\n".join(out)
