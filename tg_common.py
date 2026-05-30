"""Telegram: каналы, credentials, fetch — для parser.py и parse_tg.py."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

BASE_DIR = Path(__file__).resolve().parent
TG_SOURCES = BASE_DIR / "tg_sources.txt"
SOURCES_FILE = BASE_DIR / "sources.txt"


def normalize_channel(channel: str) -> str:
    ch = channel.strip()
    if ch.startswith("https://t.me/"):
        ch = ch.rstrip("/").rsplit("/", 1)[-1]
    if ch.startswith("t.me/"):
        ch = ch.split("/", 1)[-1]
    if not ch.startswith("@") and not ch.lstrip("-").isdigit():
        ch = f"@{ch.lstrip('@')}"
    return ch


def _config_channels() -> list[str]:
    try:
        import parse_tg as pt

        return list(pt.cfg("channels") or [])
    except Exception:
        return []


def load_tg_channels(extra: list[str] | None = None) -> list[str]:
    channels: list[str] = []
    if extra:
        channels.extend(extra)
    channels.extend(_config_channels())
    raw = os.environ.get("TG_CHANNELS", "").strip()
    if raw:
        channels.extend(x.strip() for x in raw.split(",") if x.strip())
    if TG_SOURCES.is_file():
        for line in TG_SOURCES.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                channels.append(line)
    seen: set[str] = set()
    uniq: list[str] = []
    for ch in channels:
        key = ch.lower().lstrip("@")
        if key not in seen:
            seen.add(key)
            uniq.append(ch)
    return uniq


def sync_tg_sources() -> list[str]:
    """CONFIG channels → tg_sources.txt (единый список)."""
    channels = load_tg_channels()
    if not channels:
        return []

    lines = [
        "# Telegram-каналы (auto-sync из parse_tg CONFIG + env)",
        "# parser.py и parse_tg.py читают этот файл",
        "",
    ]
    lines.extend(channels)
    TG_SOURCES.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return channels


def get_tg_credentials() -> tuple[int, str, str]:
    api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    session = os.environ.get("TG_STRING_SESSION", "").strip()

    if not (api_id and api_hash and session):
        try:
            import parse_tg as pt

            if not api_id:
                api_id = str(pt.cfg("api_id") or "")
            if not api_hash:
                api_hash = (pt.cfg("api_hash") or "").strip()
            if not session:
                session = (pt.cfg("string_session") or "").strip()
        except Exception:
            pass

    try:
        aid = int(api_id) if api_id else 0
    except ValueError:
        aid = 0
    return aid, api_hash, session


def append_vless_to_sources(links: list[str]) -> int:
    """Добавить vless:// в sources.txt без дублей."""
    if not links:
        return 0
    existing: set[str] = set()
    if SOURCES_FILE.is_file():
        for line in SOURCES_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                existing.add(line)
    new_lines = [ln for ln in links if ln not in existing]
    if not new_lines:
        return 0
    with SOURCES_FILE.open("a", encoding="utf-8") as f:
        f.write("\n")
        for ln in new_lines:
            f.write(ln + "\n")
    return len(new_lines)


async def fetch_telegram_blob(
    channels: list[str] | None = None,
    limit: int | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> str:
    api_id, api_hash, session = get_tg_credentials()
    if not api_id or not api_hash or not session:
        return ""

    chs = channels if channels is not None else load_tg_channels()
    if not chs:
        return ""

    if limit is None:
        limit = int(os.environ.get("TG_MESSAGE_LIMIT", "800"))

    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        raise ImportError("telethon not installed")

    client = TelegramClient(StringSession(session), api_id, api_hash)
    parts: list[str] = []
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return ""
        for raw_ch in chs:
            ch = normalize_channel(raw_ch)
            try:
                entity = await client.get_entity(ch)
            except Exception as exc:
                if log_fn:
                    log_fn(f"[warn] {ch}: {exc}")
                continue
            n = 0
            async for msg in client.iter_messages(entity, limit=limit):
                n += 1
                text = msg.text or msg.message or getattr(msg, "raw_text", None)
                if text:
                    parts.append(text)
            if log_fn:
                log_fn(f"[info] {ch}: {n} messages")
    finally:
        await client.disconnect()

    return "\n".join(parts)
