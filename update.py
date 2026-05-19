#!/usr/bin/env python3
"""Aggregate public IPTV m3u sources, probe liveness, write live.m3u."""
from __future__ import annotations

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "sources.yaml"
OUTPUT_PATH = ROOT / "live.m3u"

EXTINF_RE = re.compile(r"^#EXTINF:[-0-9.]+\s*(.*?),(.*)$")
ATTR_RE = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) iptv-updater/1.0"
NON_HTTP_SCHEMES = ("udp://", "rtp://", "rtmp://", "rtsp://", "p2p://", "p3p://")


@dataclass
class Channel:
    name: str
    url: str
    group: str = ""
    logo: str = ""
    tvg_id: str = ""
    raw_extinf: str = ""
    latency_ms: int = 10**9
    source: str = ""

    @property
    def norm_name(self) -> str:
        s = self.name.upper().replace(" ", "").replace("-", "").replace("_", "")
        return re.sub(r"[^A-Z0-9一-鿿+]", "", s)

    @property
    def dedupe_key(self) -> tuple[str, str]:
        return (self.norm_name, self.url.strip())


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_source(url: str, timeout: int) -> str | None:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
        if r.status_code >= 400:
            print(f"  ! HTTP {r.status_code} {url}", file=sys.stderr)
            return None
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:
        print(f"  ! fetch fail {url}: {e}", file=sys.stderr)
        return None


def parse_m3u(text: str, source: str) -> list[Channel]:
    channels: list[Channel] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF"):
            m = EXTINF_RE.match(line)
            if not m:
                i += 1
                continue
            attrs_str, display_name = m.group(1), m.group(2).strip()
            attrs = dict(ATTR_RE.findall(attrs_str))
            j = i + 1
            url = None
            while j < len(lines):
                nxt = lines[j]
                if nxt.startswith("#"):
                    j += 1
                    continue
                url = nxt
                break
            if url and not url.startswith("#"):
                channels.append(Channel(
                    name=display_name or attrs.get("tvg-name", ""),
                    url=url,
                    group=attrs.get("group-title", ""),
                    logo=attrs.get("tvg-logo", ""),
                    tvg_id=attrs.get("tvg-id", ""),
                    raw_extinf=line,
                    source=source,
                ))
                i = j + 1
                continue
        i += 1
    return channels


def is_blacklisted(url: str, blacklist: list[str]) -> bool:
    if not blacklist:
        return False
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    return any(b in host for b in blacklist)


def dedupe(channels: list[Channel]) -> list[Channel]:
    seen: dict[tuple[str, str], Channel] = {}
    for c in channels:
        if not c.url or not c.name:
            continue
        seen.setdefault(c.dedupe_key, c)
    return list(seen.values())


def probe(channel: Channel, timeout: int, read_bytes: int) -> bool:
    url = channel.url
    if url.startswith(NON_HTTP_SCHEMES):
        channel.latency_ms = 5000  # de-prioritized but kept
        return True
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    t0 = time.monotonic()
    try:
        with requests.get(
            url,
            stream=True,
            timeout=timeout,
            headers={"User-Agent": UA},
            allow_redirects=True,
        ) as r:
            if r.status_code >= 400:
                return False
            chunk = next(r.iter_content(read_bytes), b"")
            if not chunk:
                return False
    except Exception:
        return False
    channel.latency_ms = int((time.monotonic() - t0) * 1000)
    return True


def probe_all(channels: list[Channel], cfg: dict) -> list[Channel]:
    probe_cfg = cfg.get("probe", {}) or {}
    timeout = int(probe_cfg.get("timeout_seconds", 3))
    workers = int(probe_cfg.get("workers", 64))
    read_bytes = int(probe_cfg.get("read_bytes", 4096))

    alive: list[Channel] = []
    total = len(channels)
    print(f"probing {total} channels (workers={workers}, timeout={timeout}s)...")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe, c, timeout, read_bytes): c for c in channels}
        for fut in as_completed(futures):
            done += 1
            if done % 200 == 0 or done == total:
                print(f"  probed {done}/{total} alive={len(alive)}")
            c = futures[fut]
            try:
                if fut.result():
                    alive.append(c)
            except Exception:
                pass
    return alive


def is_sport(channel: Channel, keywords: list[str]) -> int:
    """Return sport rank: lower = more preferred; 10**6 if not sport."""
    hay = (channel.name + " " + channel.group).upper()
    for idx, kw in enumerate(keywords):
        if kw.upper() in hay:
            return idx
    return 10**6


def sort_channels(channels: list[Channel], keywords: list[str]) -> list[Channel]:
    return sorted(channels, key=lambda c: (is_sport(c, keywords), c.latency_ms, c.name))


def render_m3u(channels: list[Channel], keywords: list[str]) -> str:
    lines = ["#EXTM3U"]
    for c in channels:
        group = "体育" if is_sport(c, keywords) < 10**6 else (c.group or "其他")
        attrs = []
        if c.tvg_id:
            attrs.append(f'tvg-id="{c.tvg_id}"')
        attrs.append(f'tvg-name="{c.name}"')
        if c.logo:
            attrs.append(f'tvg-logo="{c.logo}"')
        attrs.append(f'group-title="{group}"')
        lines.append(f"#EXTINF:-1 {' '.join(attrs)},{c.name}")
        lines.append(c.url)
    return "\n".join(lines) + "\n"


def main() -> int:
    cfg = load_config()
    sources = cfg.get("sources") or []
    keywords = cfg.get("sports_keywords") or []
    blacklist = cfg.get("blacklist_domains") or []
    fetch_timeout = int((cfg.get("probe") or {}).get("fetch_timeout_seconds", 10))

    if not sources:
        print("no sources configured", file=sys.stderr)
        return 1

    raw: list[Channel] = []
    ok_sources = 0
    for src in sources:
        print(f"fetching {src}")
        text = fetch_source(src, fetch_timeout)
        if not text:
            continue
        ok_sources += 1
        parsed = parse_m3u(text, src)
        print(f"  parsed {len(parsed)} channels")
        raw.extend(parsed)

    if ok_sources == 0:
        print("all upstream sources failed", file=sys.stderr)
        return 1

    filtered = [c for c in raw if not is_blacklisted(c.url, blacklist)]
    print(f"after blacklist: {len(filtered)} (was {len(raw)})")

    deduped = dedupe(filtered)
    print(f"after dedupe: {len(deduped)}")

    alive = probe_all(deduped, cfg)
    print(f"alive: {len(alive)}/{len(deduped)}")

    if not alive:
        print("no live channels — refusing to overwrite live.m3u", file=sys.stderr)
        return 1

    sorted_ch = sort_channels(alive, keywords)
    output = render_m3u(sorted_ch, keywords)
    OUTPUT_PATH.write_text(output, encoding="utf-8")
    sports_count = sum(1 for c in sorted_ch if is_sport(c, keywords) < 10**6)
    print(f"wrote {OUTPUT_PATH} ({len(sorted_ch)} channels, {sports_count} sports)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
