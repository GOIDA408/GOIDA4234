#!/usr/bin/env python3
"""
Парсинг vless:// из Telegram-каналов (Telethon).

Запуск: python parse_tg.py
  — если нет сессии, спросит номер телефона и код из Telegram
  — сессия сохранится в CONFIG автоматически

Опции:  -p +79...   -c @channel   --login (только вход)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import html
import os
import re
import sys
from pathlib import Path

try:
    import telethon  # noqa: F401
except ImportError:
    print(
        f"[error] telethon не найден для {sys.executable}\n"
        f"  установи: {sys.executable} -m pip install telethon",
        flush=True,
    )
    raise SystemExit(1)

# ===========================================================================
# НАСТРОЙКИ — укажи здесь (my.telegram.org/apps)
# ===========================================================================
CONFIG = {
    "api_id": 37612132,
    "api_hash": "a402c3c1a87ebe0bb5181bea4c98daa3",
    "string_session": "",
    "channels": [
        "@cvedc_vpn",
        "@freeinternet_byMygalaru",
    ],
    "message_limit": 800,                 # сообщений с каждого канала
    "output": "tg_vless.txt",             # файл с vless://
}
# ===========================================================================

BASE_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = Path(__file__).resolve()
TG_SOURCES = BASE_DIR / "tg_sources.txt"
DEFAULT_OUT = BASE_DIR / CONFIG.get("output", "tg_vless.txt")

VLESS_RE = re.compile(r"vless://[^\s\"'<>]+", re.I)


def cfg(key: str, default=None):
    """CONFIG → env (env важнее)."""
    env_map = {
        "api_id": "TG_API_ID",
        "api_hash": "TG_API_HASH",
        "string_session": "TG_STRING_SESSION",
        "message_limit": "TG_MESSAGE_LIMIT",
    }
    env_key = env_map.get(key)
    if env_key:
        val = os.environ.get(env_key, "").strip()
        if val:
            if key == "api_id":
                return int(val)
            if key == "message_limit":
                return int(val)
            return val
    val = CONFIG.get(key, default)
    if key == "api_id" and val:
        return int(val)
    return val


def print_msg(*args, **kwargs):
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)


def load_channels(cli_channels: list[str] | None) -> list[str]:
    from tg_common import load_tg_channels

    extra = list(cfg("channels") or [])
    if cli_channels:
        extra.extend(cli_channels)
    return load_tg_channels(extra)


def normalize_channel(channel: str) -> str:
    from tg_common import normalize_channel as _norm

    return _norm(channel)


def b64_decode(text: str) -> str:
    t = re.sub(r"\s+", "", text.strip())
    t += "=" * ((4 - len(t) % 4) % 4)
    return base64.b64decode(t, validate=False).decode("utf-8", errors="ignore")


def clean_link(raw: str) -> str:
    s = html.unescape(raw.strip())
    s = s.replace("&amp;", "&").replace("&#38;", "&")
    return re.sub(r"\s+", "", s)


def extract_vless(blob: str) -> list[str]:
    found = VLESS_RE.findall(blob)
    try:
        found.extend(VLESS_RE.findall(b64_decode(blob)))
    except (ValueError, binascii.Error):
        pass
    seen: set[str] = set()
    uniq: list[str] = []
    for link in found:
        link = clean_link(link)
        if not link.lower().startswith("vless://"):
            continue
        key = link.split("#", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(link)
    return uniq


async def fetch_messages(channels: list[str], limit: int) -> str:
    from tg_common import fetch_telegram_blob

    if not cfg("api_id") or not cfg("api_hash"):
        print_msg("[error] заполни CONFIG api_id / api_hash")
        sys.exit(1)

    return await fetch_telegram_blob(channels, limit, log_fn=print_msg)


def save_session_to_config(session: str) -> None:
    """Записать string_session в parse_tg.py."""
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    new_line = f'    "string_session": "{session}",'
    if re.search(r'"string_session"\s*:', text):
        text = re.sub(
            r'^\s*"string_session"\s*:\s*".*?"\s*,?\s*(#.*)?$',
            new_line,
            text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        text = text.replace(
            '"api_hash":',
            f'"api_hash":\n{new_line}',
            1,
        )
    SCRIPT_PATH.write_text(text, encoding="utf-8")
    CONFIG["string_session"] = session
    print_msg("[ok] string_session сохранён в parse_tg.py")


def ask_phone(phone: str | None) -> str:
    if phone:
        return phone.strip()
    phone = os.environ.get("TG_PHONE", "").strip()
    if phone:
        return phone
    return input("Номер телефона (международный, +79001234567): ").strip()


def login_sync(phone: str | None = None) -> str:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = cfg("api_id")
    api_hash = (cfg("api_hash") or "").strip()
    if not api_id or not api_hash:
        print_msg("[error] заполни CONFIG api_id / api_hash")
        sys.exit(1)

    phone = ask_phone(phone)
    if not phone:
        print_msg("[error] номер телефона не указан")
        sys.exit(1)

    print_msg(f"[info] вход: {phone} (код придёт в Telegram)...")
    client = TelegramClient(StringSession(), api_id, api_hash)
    client.start(phone=phone)
    session = client.session.save()
    client.disconnect()
    save_session_to_config(session)
    return session


async def ensure_authorized(phone: str | None = None) -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = cfg("api_id")
    api_hash = (cfg("api_hash") or "").strip()
    session_str = (cfg("string_session") or "").strip()

    if session_str:
        client = TelegramClient(StringSession(session_str), api_id, api_hash)
        await client.connect()
        try:
            if await client.is_user_authorized():
                print_msg("[info] Telegram: сессия OK")
                return
        finally:
            await client.disconnect()

    print_msg("[info] Telegram: нужен вход")
    await asyncio.to_thread(login_sync, phone)


def do_login(phone: str | None = None) -> None:
    login_sync(phone)
    print_msg("[ok] можно запускать: python parse_tg.py")


def main() -> int:
    parser = argparse.ArgumentParser(description="Парсинг vless:// из Telegram-каналов")
    parser.add_argument(
        "--login",
        action="store_true",
        help="первый вход в Telegram (получить string_session)",
    )
    parser.add_argument(
        "-p",
        "--phone",
        help="номер телефона (+79...), иначе спросит после запуска",
    )
    parser.add_argument(
        "-c",
        "--channel",
        action="append",
        dest="channels",
        help="канал (@name или ссылка), можно несколько раз",
    )
    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=cfg("message_limit") or 800,
        help="сколько последних сообщений читать с канала",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUT,
        help=f"куда сохранить vless (default: {DEFAULT_OUT.name})",
    )
    parser.add_argument(
        "--append-sources",
        action="store_true",
        help="добавить ссылки в sources.txt (без дублей)",
    )
    args = parser.parse_args()

    if args.login:
        do_login(args.phone)
        return 0

    from tg_common import sync_tg_sources

    synced = sync_tg_sources()
    if synced:
        print_msg(f"[info] tg_sources.txt: {len(synced)} channel(s)")

    asyncio.run(ensure_authorized(args.phone))

    channels = load_channels(args.channels)
    if not channels:
        print_msg("[error] нет каналов — добавь в CONFIG channels или tg_sources.txt")
        return 1

    print_msg(f"[info] channels: {len(channels)}, limit={args.limit}/channel")
    blob = asyncio.run(fetch_messages(channels, args.limit))
    links = extract_vless(blob)
    print_msg(f"[info] found {len(links)} unique vless links")

    if not links:
        print_msg("[warn] vless не найдены")
        return 1

    body = "\n".join(links) + "\n"
    args.output.write_text(body, encoding="utf-8")
    print_msg(f"[ok] saved → {args.output}")

    if args.append_sources:
        from tg_common import append_vless_to_sources

        n = append_vless_to_sources(links)
        if n:
            print_msg(f"[ok] +{n} links appended to sources.txt")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print_msg("\n[info] stopped")
        raise SystemExit(130)
