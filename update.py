#!/usr/bin/env python3
"""Aggregate public IPTV m3u sources, probe liveness, write live.m3u."""
from __future__ import annotations

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "sources.yaml"
OUTPUT_PATH = ROOT / "live.m3u"

EXTINF_RE = re.compile(r"^#EXTINF:[-0-9.]+\s*(.*?),(.*)$")
ATTR_RE = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) iptv-updater/1.0"
NON_HTTP_SCHEMES = ("udp://", "rtp://", "rtmp://", "rtsp://", "p2p://", "p3p://")


COUNTRY_RE = re.compile(r"\.([a-z]{2})@", re.IGNORECASE)


@dataclass
class Channel:
    name: str
    url: str
    group: str = ""
    logo: str = ""
    tvg_id: str = ""
    raw_extinf: str = ""
    latency_ms: int = 10**9
    throughput_kbps: int = 0
    source: str = ""

    @property
    def norm_name(self) -> str:
        # Strip parenthesized/bracketed qualifiers like "(720p)" / "[Not 24/7]"
        # so CCTV-5 and CCTV-5 (720p) collapse to the same channel
        s = re.sub(r"[\(\[].*?[\)\]]", "", self.name)
        s = s.upper().replace(" ", "").replace("-", "").replace("_", "")
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


def first_hls_url(text: str, base: str) -> str | None:
    """First non-comment URL in an HLS playlist body, resolved against base."""
    if not text.startswith("#EXTM3U"):
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        return urljoin(base, line)
    return None


def _is_playlist_path(url: str) -> bool:
    p = urlparse(url).path.lower()
    return p.endswith(".m3u8") or p.endswith(".m3u")


def _measure_throughput(url: str, timeout: int, target_bytes: int,
                        headers: dict) -> tuple[int, int] | None:
    """Return (kbps, elapsed_ms) or None on failure."""
    t0 = time.monotonic()
    total = 0
    try:
        with requests.get(url, stream=True, timeout=timeout,
                          headers=headers, allow_redirects=True) as r:
            if r.status_code >= 400:
                return None
            for chunk in r.iter_content(16384):
                if not chunk:
                    continue
                total += len(chunk)
                if total >= target_bytes:
                    break
                if time.monotonic() - t0 > timeout:
                    break
    except Exception:
        return None
    elapsed = max(time.monotonic() - t0, 0.001)
    if total < 16384:
        return None
    return int((total / 1024.0) / elapsed), int(elapsed * 1000)


def probe(channel: Channel, timeout: int, segment_read_bytes: int,
          min_kbps: int, max_latency_ms: int, keep_non_http: bool) -> bool:
    """HLS-aware liveness + throughput probe.

    Single stream connection peeks the first 8KB:
      - body starts with #EXTM3U → it's a playlist, drill to find a segment
      - else → it's a media stream, continue reading on the same connection to measure KB/s.
    Direct streams cost one HTTP request; master playlists cost three.
    """
    url = channel.url
    if url.startswith(NON_HTTP_SCHEMES):
        if not keep_non_http:
            return False
        channel.latency_ms = 5000
        channel.throughput_kbps = 0
        return True
    if not (url.startswith("http://") or url.startswith("https://")):
        return False

    headers = {"User-Agent": UA}
    t0 = time.monotonic()
    try:
        r = requests.get(url, stream=True, timeout=timeout,
                         headers=headers, allow_redirects=True)
    except Exception:
        return False

    with r:
        if r.status_code >= 400:
            return False
        try:
            first = next(r.iter_content(8192), b"")
        except Exception:
            return False
        if not first:
            return False

        is_hls = first[:32].lstrip().startswith(b"#EXTM3U")

        if not is_hls:
            # Direct media stream — keep reading on the same connection to measure throughput
            total = len(first)
            try:
                for chunk in r.iter_content(16384):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total >= segment_read_bytes:
                        break
                    if time.monotonic() - t0 > timeout:
                        break
            except Exception:
                return False
            elapsed = max(time.monotonic() - t0, 0.001)
            if total < 16384:
                return False
            kbps = int((total / 1024.0) / elapsed)
            if kbps < min_kbps:
                return False
            channel.latency_ms = int(elapsed * 1000)
            channel.throughput_kbps = kbps
            return True

        # HLS playlist — read rest of body (playlists are small, cap 256KB)
        try:
            rest = b""
            for chunk in r.iter_content(8192):
                if chunk:
                    rest += chunk
                    if len(rest) > 256 * 1024:
                        break
            body = (first + rest).decode("utf-8", errors="ignore")
        except Exception:
            return False

    init_ms = int((time.monotonic() - t0) * 1000)
    if init_ms > max_latency_ms:
        return False

    # Drill: master → media → segment (1 extra hop max)
    nxt = first_hls_url(body, url)
    if not nxt:
        return False
    final_url = nxt
    if _is_playlist_path(nxt):
        try:
            r2 = requests.get(nxt, timeout=timeout, headers=headers, allow_redirects=True)
            if r2.status_code >= 400:
                return False
            body2 = r2.text
        except Exception:
            return False
        if body2.startswith("#EXTM3U"):
            nxt2 = first_hls_url(body2, final_url)
            if not nxt2:
                return False
            final_url = nxt2

    result = _measure_throughput(final_url, timeout, segment_read_bytes, headers)
    if not result:
        return False
    kbps, _ = result
    if kbps < min_kbps:
        return False

    channel.latency_ms = init_ms
    channel.throughput_kbps = kbps
    return True


def probe_all(channels: list[Channel], cfg: dict) -> list[Channel]:
    probe_cfg = cfg.get("probe", {}) or {}
    timeout = int(probe_cfg.get("timeout_seconds", 3))
    workers = int(probe_cfg.get("workers", 32))
    segment_read_bytes = int(probe_cfg.get("segment_read_bytes", 65536))
    min_kbps = int(probe_cfg.get("min_kbps", 200))
    max_latency_ms = int(probe_cfg.get("max_latency_ms", 2500))
    keep_non_http = bool(probe_cfg.get("keep_non_http", False))

    alive: list[Channel] = []
    total = len(channels)
    print(f"probing {total} channels (workers={workers}, timeout={timeout}s, "
          f"min={min_kbps}KB/s, max_latency={max_latency_ms}ms)...")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                probe, c, timeout, segment_read_bytes,
                min_kbps, max_latency_ms, keep_non_http,
            ): c for c in channels
        }
        for fut in as_completed(futures):
            done += 1
            if done % 100 == 0 or done == total:
                print(f"  probed {done}/{total} alive={len(alive)}")
            c = futures[fut]
            try:
                if fut.result():
                    alive.append(c)
            except Exception:
                pass
    return alive


def filter_country(channels: list[Channel], allowed: list[str]) -> list[Channel]:
    """Drop channels whose tvg-id explicitly marks a non-allowed country.

    Pattern in iptv-org IDs: 'XYZ.cn@SD', 'JSPORTS3.jp@SD' — match `.{cc}@`.
    Channels without a country marker pass through (national sources rarely set tvg-id).
    """
    if not allowed:
        return channels
    allowed_set = {c.lower() for c in allowed}
    out: list[Channel] = []
    for c in channels:
        m = COUNTRY_RE.search(c.tvg_id or "")
        if m:
            if m.group(1).lower() in allowed_set:
                out.append(c)
        else:
            out.append(c)
    return out


def filter_whitelist(channels: list[Channel], mode: str, keywords: list[str]) -> list[Channel]:
    if mode != "whitelist" or not keywords:
        return channels
    kws = [k.upper() for k in keywords if k]
    out: list[Channel] = []
    for c in channels:
        hay = (c.name + " " + c.group).upper()
        if any(kw in hay for kw in kws):
            out.append(c)
    return out


def limit_per_channel(channels: list[Channel], max_per: int) -> list[Channel]:
    """channels must already be sorted in priority order."""
    if not max_per or max_per <= 0:
        return channels
    counts: dict[str, int] = {}
    out: list[Channel] = []
    for c in channels:
        n = counts.get(c.norm_name, 0)
        if n < max_per:
            out.append(c)
            counts[c.norm_name] = n + 1
    return out


def is_sport(channel: Channel, keywords: list[str]) -> int:
    """Return sport rank: lower = more preferred; 10**6 if not sport."""
    hay = (channel.name + " " + channel.group).upper()
    for idx, kw in enumerate(keywords):
        if kw.upper() in hay:
            return idx
    return 10**6


def sort_channels(channels: list[Channel], keywords: list[str]) -> list[Channel]:
    # Sports first (by keyword index), then highest throughput, then lowest latency, then name
    return sorted(
        channels,
        key=lambda c: (is_sport(c, keywords), -c.throughput_kbps, c.latency_ms, c.name),
    )


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

    filter_cfg = cfg.get("filter") or {}
    mode = filter_cfg.get("mode", "all")
    wl_keywords = filter_cfg.get("keywords") or []
    if mode == "whitelist":
        filtered = filter_whitelist(filtered, mode, wl_keywords)
        print(f"after whitelist ({len(wl_keywords)} keywords): {len(filtered)}")

    allowed_countries = filter_cfg.get("allowed_countries") or []
    if allowed_countries:
        before = len(filtered)
        filtered = filter_country(filtered, allowed_countries)
        print(f"after country filter ({allowed_countries}): {len(filtered)} (was {before})")

    deduped = dedupe(filtered)
    print(f"after dedupe: {len(deduped)}")

    alive = probe_all(deduped, cfg)
    print(f"alive: {len(alive)}/{len(deduped)}")

    if not alive:
        print("no live channels — refusing to overwrite live.m3u", file=sys.stderr)
        return 1

    sorted_ch = sort_channels(alive, keywords)

    limits = cfg.get("limits") or {}
    max_per = int(limits.get("max_per_channel", 0))
    if max_per > 0:
        before = len(sorted_ch)
        sorted_ch = limit_per_channel(sorted_ch, max_per)
        print(f"after per-channel cap (max={max_per}): {len(sorted_ch)} (was {before})")

    output = render_m3u(sorted_ch, keywords)
    OUTPUT_PATH.write_text(output, encoding="utf-8")
    sports_count = sum(1 for c in sorted_ch if is_sport(c, keywords) < 10**6)
    if sorted_ch:
        avg_kbps = sum(c.throughput_kbps for c in sorted_ch) // len(sorted_ch)
        median_kbps = sorted(c.throughput_kbps for c in sorted_ch)[len(sorted_ch) // 2]
    else:
        avg_kbps = median_kbps = 0
    print(f"wrote {OUTPUT_PATH} ({len(sorted_ch)} channels, {sports_count} sports, "
          f"avg {avg_kbps} KB/s, median {median_kbps} KB/s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
