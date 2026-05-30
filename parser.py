#!/usr/bin/env python3
"""
VLESS parser + batch Xray checker.
- Источники: sources.txt + Telegram (tg_sources.txt, Telethon)
- Whitelist (RU SNI) + Global — проверка через Xray batch
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.parse
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional

import aiohttp
import requests
from aiohttp import ClientConnectorError, ClientError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
XRAY_DIR = BASE_DIR / "xray"

NEEDED_WHITELIST = int(os.getenv("NEEDED_WHITELIST", "100"))
NEEDED_FOREIGN = int(os.getenv("NEEDED_FOREIGN", "50"))
WHITELIST_SCAN_MAX = int(os.getenv("WHITELIST_SCAN_MAX", "15000"))
FOREIGN_SCAN_MAX = int(os.getenv("FOREIGN_SCAN_MAX", "12000"))
FETCH_WORKERS = int(os.getenv("FETCH_WORKERS", "32"))
XRAY_GROUP_SIZE = int(os.getenv("XRAY_GROUP_SIZE", "1000"))
XRAY_MIN_SPLIT = int(os.getenv("XRAY_MIN_SPLIT", "2"))
XRAY_START_DELAY = float(os.getenv("XRAY_START_DELAY", "0.5"))
REQUEST_CONCURRENCY = int(os.getenv("REQUEST_CONCURRENCY", "150"))
PROXY_TIMEOUT = float(os.getenv("PROXY_TIMEOUT", "10"))
MAX_HTTP_MS = int(os.getenv("MAX_HTTP_MS", "600"))
PREFERRED_MS = int(os.getenv("PREFERRED_MS", str(MAX_HTTP_MS)))
PROBE_URL = os.getenv("PROBE_URL", "https://www.gstatic.com/generate_204")
WHITELIST_PROBE_URL = os.getenv("WHITELIST_PROBE_URL", "http://www.rt.ru")
STOP_ALIVE_BUFFER = int(os.getenv("STOP_ALIVE_BUFFER", "25"))
BASE_PORT = int(os.getenv("BASE_PORT", "15000"))

_xray_multi_env = os.getenv("XRAY_MULTI", "").strip().lower()
if _xray_multi_env:
    XRAY_MULTI = _xray_multi_env in ("1", "true", "yes")
else:
    XRAY_MULTI = False  # по умолчанию: 1 xray = 1 пачка до XRAY_GROUP_SIZE inbounds
XRAY_CONCURRENCY = max(1, int(os.getenv("XRAY_CONCURRENCY", "100")))
XRAY_SHOW_WINDOW = os.getenv("XRAY_SHOW_WINDOW", "0").lower() in ("1", "true", "yes")

RU_FALLBACK = {
    "ozon.ru", "ya.ru", "yandex.ru", "vk.com", "sberbank.ru", "mail.ru",
    "dzen.ru", "gosuslugi.ru", "avito.ru", "wildberries.ru", "wb.ru",
    "rt.ru", "mts.ru", "beeline.ru", "megafon.ru", "tele2.ru",
}

RU_WHITELIST_URL = (
    "https://raw.githubusercontent.com/hxehex/russia-mobile-internet-whitelist/main/whitelist.txt"
)

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
VLESS_RE = re.compile(r"vless://[^\s\"'<>]+", re.I)

_XRAY_PATH: str | None = None
_XRAY_LAUNCH_SEM: asyncio.Semaphore | None = None


def xray_launch_sem() -> asyncio.Semaphore:
    global _XRAY_LAUNCH_SEM
    if _XRAY_LAUNCH_SEM is None:
        _XRAY_LAUNCH_SEM = asyncio.Semaphore(XRAY_CONCURRENCY)
    return _XRAY_LAUNCH_SEM


def log(level: str, msg: str) -> None:
    print(f"[{level}] {msg}", flush=True)


def setup_logs() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(line_buffering=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class VlessNode:
    original: str
    uuid: str
    host: str
    port: int
    query: dict
    name: str

    @property
    def security(self) -> str:
        return self.query.get("security", "none")

    @property
    def type(self) -> str:
        return self.query.get("type", "tcp")

    @property
    def pbk(self) -> str:
        return self.query.get("pbk", "")

    @property
    def sni(self) -> str:
        return self.query.get("sni") or self.query.get("peer") or self.host

    @property
    def fingerprint(self) -> str:
        return self.query.get("fp", "") or "chrome"


# ---------------------------------------------------------------------------
# Xray
# ---------------------------------------------------------------------------

def _xray_zip_url() -> str:
    system = platform.system()
    if system == "Windows":
        return "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-windows-64.zip"
    if system == "Darwin":
        machine = platform.machine().lower()
        if machine in ("arm64", "aarch64"):
            return (
                "https://github.com/XTLS/Xray-core/releases/latest/download/"
                "Xray-macos-arm64-v8a.zip"
            )
        return "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-macos-64.zip"
    return "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip"


def _xray_bin_name() -> str:
    return "xray.exe" if platform.system() == "Windows" else "xray"


def download_xray() -> str:
    XRAY_DIR.mkdir(parents=True, exist_ok=True)
    bin_name = _xray_bin_name()
    dest_bin = XRAY_DIR / bin_name
    url = _xray_zip_url()

    log("info", f"Downloading Xray: {url}")
    resp = requests.get(url, timeout=180)
    resp.raise_for_status()

    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        for member in zf.namelist():
            base = os.path.basename(member)
            if base in {bin_name, "xray", "xray.exe", "geoip.dat", "geosite.dat"}:
                target = XRAY_DIR / (bin_name if base in ("xray", "xray.exe") else base)
                with zf.open(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())

    if not dest_bin.is_file() and (XRAY_DIR / "xray").is_file():
        shutil.move(str(XRAY_DIR / "xray"), str(dest_bin))

    if not dest_bin.is_file():
        raise FileNotFoundError(f"xray binary not found after download from {url}")

    if platform.system() != "Windows":
        os.chmod(dest_bin, 0o755)

    log("info", f"Xray ready: {dest_bin}")
    return str(dest_bin)


def find_xray() -> str:
    env_bin = (os.environ.get("XRAY_BIN") or os.environ.get("XRAY") or "").strip()
    if env_bin and os.path.isfile(env_bin):
        return os.path.abspath(env_bin)

    bin_name = _xray_bin_name()
    for d in (XRAY_DIR, BASE_DIR, Path.cwd()):
        path = d / bin_name
        if path.is_file():
            return str(path.resolve())

    which = shutil.which(bin_name)
    if which:
        return which

    raise FileNotFoundError(
        "xray not found — set XRAY_BIN or place binary in ./xray/"
    )


def get_xray_path() -> str:
    global _XRAY_PATH
    if _XRAY_PATH is None:
        try:
            _XRAY_PATH = find_xray()
        except FileNotFoundError:
            auto = os.environ.get("XRAY_AUTO_DOWNLOAD", "1").lower()
            if auto in ("0", "false", "no"):
                raise
            _XRAY_PATH = download_xray()
    return _XRAY_PATH


def xray_cwd() -> str:
    if (XRAY_DIR / "geoip.dat").is_file():
        return str(XRAY_DIR)
    parent = os.path.dirname(os.path.abspath(get_xray_path()))
    return parent or str(BASE_DIR)


def xray_cleanup() -> None:
    for pattern in ("xray_config_*.json", "batch_*.json", "batch_*.log"):
        for path in BASE_DIR.glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass

    try:
        if platform.system() == "Windows":
            subprocess.run(
                "taskkill /f /im xray.exe",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.run(
                "pkill -f xray",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass


def stop_process(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    try:
        proc.terminate()
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass
    except Exception:
        pass


def ensure_xray() -> bool:
    try:
        get_xray_path()
        return True
    except FileNotFoundError as exc:
        log("error", str(exc))
        return False


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def load_ru_domains() -> set[str]:
    domains = set(RU_FALLBACK)
    try:
        r = requests.get(RU_WHITELIST_URL, timeout=15)
        if r.status_code == 200:
            for line in r.text.splitlines():
                d = line.strip().lower().strip(".")
                if d and not d.startswith("#") and "." in d:
                    domains.add(d)
    except requests.RequestException as exc:
        log("warn", f"RU domains fallback: {exc}")
    log("info", f"RU domains: {len(domains)}")
    return domains


def load_sources() -> list[str]:
    path = BASE_DIR / "sources.txt"
    urls: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and line.startswith("http") and not line.startswith("#"):
                urls.add(line)
    return list(urls)


async def fetch_url(session: aiohttp.ClientSession, url: str) -> str:
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=12),
            headers={"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36"},
        ) as resp:
            if resp.status == 200:
                from sub_decode import unwrap_subscription

                raw = await resp.text()
                return unwrap_subscription(raw)
    except Exception:
        pass
    return ""


async def fetch_all(urls: list[str]) -> list[str]:
    results: list[str] = []
    connector = aiohttp.TCPConnector(limit=FETCH_WORKERS)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [fetch_url(session, url) for url in urls]
        for idx, task in enumerate(asyncio.as_completed(tasks), 1):
            res = await task
            if res:
                results.append(res)
            if idx % 50 == 0 or idx == len(tasks):
                log("info", f"fetched {idx}/{len(urls)} sources")
    return results


async def fetch_telegram_text() -> str:
    try:
        from tg_common import fetch_telegram_blob, get_tg_credentials, load_tg_channels
    except ImportError:
        log("warn", "telegram: tg_common not found")
        return ""

    api_id, api_hash, session = get_tg_credentials()
    if not api_id or not api_hash:
        log("info", "telegram: skip (TG_API_ID / TG_API_HASH)")
        return ""
    if not session:
        log("info", "telegram: skip (TG_STRING_SESSION)")
        return ""

    channels = load_tg_channels()
    if not channels:
        log("info", "telegram: skip (tg_sources.txt empty)")
        return ""

    try:
        blob = await fetch_telegram_blob(channels)
    except ImportError:
        log("warn", "telegram: pip install telethon")
        return ""
    except Exception as exc:
        log("warn", f"telegram: {exc}")
        return ""

    if not blob:
        log("warn", "telegram: empty response")
        return ""

    n_links = len(extract_links(blob))
    log("info", f"telegram: {n_links} vless from {len(channels)} channel(s)")

    if os.getenv("TG_APPEND_SOURCES", "1").lower() in ("1", "true", "yes"):
        from tg_common import append_vless_to_sources

        added = append_vless_to_sources(extract_links(blob))
        if added:
            log("info", f"telegram: +{added} vless → sources.txt")

    return blob


# ---------------------------------------------------------------------------
# Extract / filter
# ---------------------------------------------------------------------------

def decode_base64_blob(blob: str) -> str:
    try:
        clean_blob = re.sub(r"\s+", "", blob)
        padded = clean_blob + "=" * ((4 - len(clean_blob) % 4) % 4)
        return base64.b64decode(padded, validate=False).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def extract_links(text: str) -> list[str]:
    from sub_decode import unwrap_subscription

    blob = unwrap_subscription(text)
    links = set(VLESS_RE.findall(blob))
    # на случай vless внутри необработанных кусков исходника
    links.update(VLESS_RE.findall(text))
    decoded = decode_base64_blob(blob)
    if decoded:
        links.update(VLESS_RE.findall(decoded))

    cleaned: set[str] = set()
    for link in links:
        link = html.unescape(link)
        link = link.replace("&amp;", "&").replace("&#38;", "&")
        link = re.sub(r"\s+", "", link)
        if link.lower().startswith("vless://"):
            cleaned.add(link)
    return list(cleaned)


def parse_vless(link: str) -> Optional[VlessNode]:
    try:
        if not link.lower().startswith("vless://"):
            return None

        main_part, *name_part = link.split("#", 1)
        name = urllib.parse.unquote(name_part[0]) if name_part else ""

        main_part = main_part[8:]
        user_host, *query_part = main_part.split("?", 1)
        if "@" not in user_host:
            return None
        uuid, host_port = user_host.split("@", 1)

        if "]" in host_port:
            host_end = host_port.rfind("]") + 1
            host = host_port[:host_end].strip("[]")
            port_str = host_port[host_end:].lstrip(":")
        else:
            if ":" not in host_port:
                return None
            host, port_str = host_port.rsplit(":", 1)

        port = int(port_str)

        query: dict[str, str] = {}
        if query_part:
            for q in query_part[0].split("&"):
                if "=" in q:
                    k, v = q.split("=", 1)
                    query[k] = urllib.parse.unquote(v)

        return VlessNode(link, uuid, host, port, query, name)
    except Exception:
        return None


def accept_vless(node: Optional[VlessNode]) -> bool:
    """Минимум проверок — живость только через Xray + ping."""
    if not node:
        return False
    if not UUID_RE.match(node.uuid.strip()):
        return False
    if not node.host or not (1 <= node.port <= 65535):
        return False
    return True


def ok_for_output(node: VlessNode) -> bool:
    """В подписку — только reality/tls (CI)."""
    return node.security.lower() in ("reality", "tls")


def split_pools(
    nodes: list[VlessNode], ru_domains: set[str]
) -> tuple[list[VlessNode], list[VlessNode]]:
    whitelist: list[VlessNode] = []
    global_pool: list[VlessNode] = []

    for node in nodes:
        is_ru = False
        checks = [
            node.sni,
            node.host,
            node.query.get("host", ""),
            node.query.get("peer", ""),
        ]
        for c in checks:
            c = (c or "").lower()
            if c and any(c == d or c.endswith("." + d) for d in ru_domains):
                is_ru = True
                break
        (whitelist if is_ru else global_pool).append(node)

    log("info", f"pools: whitelist={len(whitelist)} global={len(global_pool)}")
    return whitelist, global_pool


def collect_nodes(texts: list[str]) -> list[VlessNode]:
    raw_links: set[str] = set()
    for text in texts:
        raw_links.update(extract_links(text))

    log("info", f"extracted {len(raw_links)} raw vless links")

    parsed: list[VlessNode] = []
    seen_uri: set[str] = set()
    skipped_parse = 0

    for link in raw_links:
        node = parse_vless(link)
        if not accept_vless(node):
            skipped_parse += 1
            continue
        base_uri = node.original.split("#", 1)[0]
        if base_uri in seen_uri:
            continue
        seen_uri.add(base_uri)
        parsed.append(node)

    log(
        "info",
        f"queued {len(parsed)} nodes for ping test "
        f"(skip parse={skipped_parse}, dedup={len(raw_links) - len(parsed) - skipped_parse})",
    )
    return parsed


# ---------------------------------------------------------------------------
# Xray config + checking
# ---------------------------------------------------------------------------

def build_stream_settings(node: VlessNode) -> dict:
    net = node.type or "tcp"
    sec = node.security.lower() if node.security else "none"
    ss: dict = {"network": net, "security": sec}

    if net == "grpc":
        ss["grpcSettings"] = {
            "serviceName": node.query.get("serviceName", node.query.get("path", "")),
        }
    elif net == "ws":
        ss["wsSettings"] = {
            "path": node.query.get("path", "/") or "/",
            "headers": {
                "Host": node.query.get("host", node.host) or node.host,
            },
        }
    elif net in ("httpupgrade", "xhttp", "splithttp"):
        ss["httpupgradeSettings"] = {
            "path": node.query.get("path", "/") or "/",
            "host": node.query.get("host", node.host) or node.host,
        }

    if sec == "reality":
        ss["realitySettings"] = {
            "publicKey": node.pbk,
            "shortId": node.query.get("sid", node.query.get("shortId", "")),
            "serverName": node.sni,
            "fingerprint": node.fingerprint,
            "spiderX": node.query.get("spx", node.query.get("spiderx", "/")) or "/",
        }
    elif sec == "tls":
        ss["tlsSettings"] = {
            "serverName": node.sni,
            "fingerprint": node.fingerprint,
            "allowInsecure": node.query.get("allowInsecure", "").lower() in ("1", "true"),
        }

    return ss


def build_xray_config(nodes: list[VlessNode], base_port: int) -> dict:
    inbounds = []
    outbounds = []
    rules = []

    for i, node in enumerate(nodes):
        user: dict = {"id": node.uuid, "encryption": "none"}
        flow = node.query.get("flow", "")
        if flow:
            user["flow"] = flow

        inbounds.append(
            {
                "tag": f"in_{i}",
                "port": base_port + i,
                "listen": "127.0.0.1",
                "protocol": "http",
                "settings": {"timeout": 0},
            }
        )
        outbounds.append(
            {
                "tag": f"out_{i}",
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": node.host,
                            "port": node.port,
                            "users": [user],
                        }
                    ]
                },
                "streamSettings": build_stream_settings(node),
            }
        )
        rules.append(
            {
                "type": "field",
                "inboundTag": [f"in_{i}"],
                "outboundTag": f"out_{i}",
            }
        )

    return {
        "log": {"loglevel": "error"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {"domainStrategy": "AsIs", "rules": rules},
    }


async def check_node(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    node: VlessNode,
    proxy_port: int,
    probe_url: str,
    stats: dict,
) -> Optional[dict]:
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    async with sem:
        start = time.time()
        try:
            async with session.get(
                probe_url,
                proxy=proxy_url,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36"},
            ) as resp:
                await resp.content.read(512)
                latency = int((time.time() - start) * 1000)
                if resp.status >= 400:
                    stats["dead"] += 1
                    return None
                if latency > MAX_HTTP_MS:
                    stats["slow"] += 1
                    return None
                return {"node": node, "latency": latency}
        except (ClientConnectorError, ClientError, asyncio.TimeoutError, OSError):
            stats["dead"] += 1
        except Exception:
            stats["dead"] += 1
    return None


async def check_batch_recursive(
    nodes: list[VlessNode],
    base_port: int,
    probe_url: str,
    sem: asyncio.Semaphore,
    stats: dict,
    group_name: str = "batch",
) -> list[dict]:
    if not nodes:
        return []

    config = build_xray_config(nodes, base_port)
    fd, cfg_path = tempfile.mkstemp(suffix=".json", prefix="xray_batch_")
    os.close(fd)

    popen_kw: dict = {
        "cwd": xray_cwd(),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt" and not XRAY_SHOW_WINDOW:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        popen_kw["startupinfo"] = si

    proc: subprocess.Popen | None = None
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False)

        log(
            "info",
            f"{group_name}: xray batch {len(nodes)} nodes, "
            f"ports {base_port}-{base_port + len(nodes) - 1}",
        )

        proc = subprocess.Popen([get_xray_path(), "-c", cfg_path], **popen_kw)
        await asyncio.sleep(XRAY_START_DELAY)

        if proc.poll() is not None:
            stderr_data = b""
            try:
                _, stderr_data = proc.communicate(timeout=2)
            except Exception:
                pass
            stop_process(proc)
            if stderr_data:
                log("warn", f"xray crash {group_name}: {stderr_data.decode(errors='ignore')[-500:]}")

            if len(nodes) > XRAY_MIN_SPLIT:
                mid = len(nodes) // 2
                left = await check_batch_recursive(
                    nodes[:mid], base_port, probe_url, sem, stats, f"{group_name}_a"
                )
                right = await check_batch_recursive(
                    nodes[mid:], base_port + mid, probe_url, sem, stats, f"{group_name}_b"
                )
                return left + right

            stats["dead"] += len(nodes)
            return []

        timeout = aiohttp.ClientTimeout(total=PROXY_TIMEOUT)
        connector = aiohttp.TCPConnector(
            limit=REQUEST_CONCURRENCY,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )

        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            tasks = [
                check_node(session, sem, node, base_port + i, probe_url, stats)
                for i, node in enumerate(nodes)
            ]
            raw = await asyncio.gather(*tasks)

        return [r for r in raw if r]

    finally:
        stop_process(proc)
        try:
            os.remove(cfg_path)
        except OSError:
            pass


async def check_nodes_group(
    nodes: list[VlessNode],
    probe_url: str,
    http_sem: asyncio.Semaphore,
    stats: dict,
    group_name: str,
) -> list[dict]:
    """Один xray на batch (Linux CI) или до XRAY_CONCURRENCY процессов xray (Windows)."""
    if not nodes:
        return []

    if not XRAY_MULTI:
        return await check_batch_recursive(
            nodes, BASE_PORT, probe_url, http_sem, stats, group_name
        )

    async def one(node: VlessNode, idx: int) -> list[dict]:
        async with xray_launch_sem():
            port = BASE_PORT + (idx % XRAY_CONCURRENCY)
            return await check_batch_recursive(
                [node], port, probe_url, http_sem, stats, f"{group_name}_{idx}"
            )

    parallel = min(len(nodes), XRAY_CONCURRENCY)
    log("info", f"{group_name}: multi-xray, {len(nodes)} nodes, up to {parallel} xray.exe")
    batches = await asyncio.gather(*(one(n, i) for i, n in enumerate(nodes)))
    return [r for batch in batches for r in batch]


async def scan_pool(
    nodes: list[VlessNode], need: int, is_white: bool, scan_max: int
) -> list[VlessNode]:
    probe_url = WHITELIST_PROBE_URL if is_white else PROBE_URL
    label = "whitelist" if is_white else "global"
    log("info", f"{label}: scan up to {min(len(nodes), scan_max)} nodes, need {need}, probe {probe_url}")

    nodes_reality = [n for n in nodes if n.security.lower() == "reality"]
    nodes_tls = [n for n in nodes if n.security.lower() == "tls"]
    nodes_other = [n for n in nodes if n.security.lower() not in ("reality", "tls")]
    random.shuffle(nodes_reality)
    random.shuffle(nodes_tls)
    random.shuffle(nodes_other)
    sorted_nodes = (nodes_reality + nodes_tls + nodes_other)[:scan_max]

    sem = asyncio.Semaphore(REQUEST_CONCURRENCY)
    alive_results: list[dict] = []
    stats = {"dead": 0, "slow": 0}

    for i in range(0, len(sorted_nodes), XRAY_GROUP_SIZE):
        output_alive = sum(1 for x in alive_results if ok_for_output(x["node"]))
        if output_alive >= need + STOP_ALIVE_BUFFER:
            log("info", f"{label}: enough alive for output ({output_alive}), stop early")
            break

        chunk = sorted_nodes[i : i + XRAY_GROUP_SIZE]
        batch_no = i // XRAY_GROUP_SIZE + 1
        group_name = f"{label}_batch{batch_no}"
        log(
            "info",
            f"{label}: batch {batch_no} — {len(chunk)} servers "
            f"({i + 1}-{i + len(chunk)}/{len(sorted_nodes)})",
        )
        res = await check_nodes_group(
            chunk, probe_url, sem, stats, group_name
        )
        alive_results.extend(res)
        log(
            "info",
            f"{label}: chunk alive={len(res)}, total={len(alive_results)}, "
            f"dead={stats['dead']}, slow>{MAX_HTTP_MS}ms={stats['slow']}",
        )

    unique: dict[str, dict] = {}
    for item in alive_results:
        node = item["node"]
        if not ok_for_output(node):
            continue
        lat = item["latency"]
        if lat > PREFERRED_MS:
            continue

        score = lat
        if node.security == "tls":
            score += 20
        if node.type == "grpc":
            score += 10
        if is_white:
            score -= 30

        key = f"{node.host}:{node.port}"
        if key not in unique or unique[key]["score"] > score:
            unique[key] = {"node": node, "score": score, "lat": lat}

    final_list = sorted(unique.values(), key=lambda x: (x["lat"], x["score"]))
    result = [x["node"] for x in final_list[:need]]
    log("info", f"{label}: finished {len(result)}/{need}")
    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _write_lines(path: Path, items: list[str]) -> None:
    body = "\n".join(items)
    if items:
        body += "\n"
    path.write_text(body, encoding="utf-8")


def _write_sub(raw_path: Path, b64_path: Path, items: list[str]) -> None:
    _write_lines(raw_path, items)
    b64 = base64.b64encode(("\n".join(items) + ("\n" if items else "")).encode()).decode()
    b64_path.write_text(b64, encoding="utf-8")


def save_results(wl: list[VlessNode], gl: list[VlessNode]) -> bool:
    wl_links = [n.original for n in wl if ok_for_output(n)]
    gl_links = [n.original for n in gl if ok_for_output(n)]

    if not wl_links and not gl_links:
        log("error", "0 alive servers — subscriptions NOT updated")
        return False

    # name.txt — все рабочие серверы (whitelist + global, без дублей)
    name_links: list[str] = []
    seen_uri: set[str] = set()
    for link in wl_links + gl_links:
        key = link.split("#", 1)[0]
        if key in seen_uri:
            continue
        seen_uri.add(key)
        name_links.append(link)

    _write_lines(BASE_DIR / "name.txt", name_links)
    _write_sub(BASE_DIR / "sub_name_raw.txt", BASE_DIR / "sub_name.txt", name_links)

    _write_lines(BASE_DIR / "whitelist.txt", wl_links)
    _write_sub(BASE_DIR / "sub_whitelist_raw.txt", BASE_DIR / "sub_whitelist.txt", wl_links)

    _write_lines(BASE_DIR / "global.txt", gl_links)
    _write_sub(BASE_DIR / "sub_global_raw.txt", BASE_DIR / "sub_global.txt", gl_links)
    _write_lines(BASE_DIR / "foreign.txt", gl_links)
    if gl_links:
        (BASE_DIR / "sub.txt").write_text(
            base64.b64encode(("\n".join(gl_links) + "\n").encode()).decode(),
            encoding="utf-8",
        )

    log(
        "done",
        f"name {len(name_links)} | whitelist {len(wl_links)}/{NEEDED_WHITELIST} | "
        f"global {len(gl_links)}/{NEEDED_FOREIGN} (ping<={PREFERRED_MS}ms)",
    )
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run() -> int:
    from tg_common import sync_tg_sources

    synced = sync_tg_sources()
    if synced:
        log("info", f"telegram channels: {', '.join(synced)}")

    domains = load_ru_domains()

    urls = load_sources()
    if not urls:
        log("warn", "sources.txt empty — only telegram")
    else:
        log("info", f"source URLs: {len(urls)}")

    texts: list[str] = []
    if urls:
        texts.extend(await fetch_all(urls))
        log("info", f"http fetched: {len(texts)} blobs")

    tg_blob = await fetch_telegram_text()
    if tg_blob:
        texts.append(tg_blob)

    if not texts:
        log("error", "no data from sources.txt or telegram")
        return 1

    parsed = collect_nodes(texts)
    if not parsed:
        log("error", "no valid vless after filter")
        return 1

    wl_pool, gl_pool = split_pools(parsed, domains)

    alive_wl = await scan_pool(wl_pool, NEEDED_WHITELIST, True, WHITELIST_SCAN_MAX)
    alive_gl = await scan_pool(gl_pool, NEEDED_FOREIGN, False, FOREIGN_SCAN_MAX)

    log("summary", f"whitelist {len(alive_wl)}/{NEEDED_WHITELIST} | global {len(alive_gl)}/{NEEDED_FOREIGN}")

    if alive_wl:
        for i, n in enumerate(alive_wl[:5], 1):
            log("summary", f"  wl {i}. {n.host}:{n.port} {n.security}")
    if alive_gl:
        for i, n in enumerate(alive_gl[:5], 1):
            log("summary", f"  gl {i}. {n.host}:{n.port} {n.security}")

    if not alive_wl and not alive_gl:
        log("error", "0 alive servers")
        return 1

    if not alive_wl or not alive_gl:
        log("warn", "one of the pools is empty")

    return 0 if save_results(alive_wl, alive_gl) else 1


def main() -> int:
    setup_logs()
    xray_cleanup()
    log(
        "info",
        f"parser + batch xray | group={XRAY_GROUP_SIZE} workers={REQUEST_CONCURRENCY} | "
        f"wl={NEEDED_WHITELIST} global={NEEDED_FOREIGN} | max_ping={PREFERRED_MS}ms",
    )
    if XRAY_MULTI:
        log("info", f"xray mode: multi (до {XRAY_CONCURRENCY} xray.exe параллельно)")
    else:
        log("info", f"xray mode: batch — 1 xray на {XRAY_GROUP_SIZE} серверов")
    log("info", f"probe global={PROBE_URL} whitelist={WHITELIST_PROBE_URL}")

    if not ensure_xray():
        return 1
    log("info", f"xray: {get_xray_path()}")

    try:
        code = asyncio.run(run())
    finally:
        xray_cleanup()

    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("info", "stopped")
        xray_cleanup()
        raise SystemExit(130)
    except Exception as exc:
        log("error", f"fatal: {exc}\n{traceback.format_exc()}")
        xray_cleanup()
        raise SystemExit(1)
