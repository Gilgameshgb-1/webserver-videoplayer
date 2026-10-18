"""Decoupled Media Acquisition Subsystem for stream.pi.

Architectural Separation:
    Authorized Media Sources / Catalogs / Magnets / Torznab
                        │
                        ▼
                acquisition.py  (Media Acquisition)
                        │
                        ▼
               config.MOVIES_DIR (Media Storage)
                        │
                        ▼
                 library.py      (Library Scanner -> Metadata + Subtitle Index)

This module is intentionally decoupled from the playback and library scanning
pipelines. Movies added manually via SMB/NFS/SCP, external drives, or through
this acquisition service all land in Media Storage and are indexed identically
by `library.py`.
"""

import json
import os
import re
import shutil
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests

import config
import release_parser

try:
    import libtorrent as lt  # type: ignore
    HAS_LIBTORRENT = True
except ImportError:
    lt = None  # type: ignore
    HAS_LIBTORRENT = False


TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
]

YTS_DOMAINS = [
    "https://yts.mx",
    "https://yts.lt",
    "https://yts.am",
    "https://yts.ag",
]

APIBAY_URL = "https://apibay.org/q.php"

_session_lock = threading.Lock()
_lt_session: Any = None
_downloads: Dict[str, Dict[str, Any]] = {}
_monitor_started = False


def _sanitize_folder_name(title: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*]', "", (title or "").strip())
    clean = re.sub(r"\s+", "_", clean).strip("._")
    return clean[:120] or f"Movie_{int(time.time())}"


def _build_magnet(info_hash: str, display_name: str) -> str:
    ih = info_hash.strip()
    if ih.startswith("magnet:?"):
        return ih
    dn = urllib.parse.quote_plus(display_name or "Movie")
    tr = "&".join(f"tr={urllib.parse.quote_plus(t)}" for t in TRACKERS)
    return f"magnet:?xt=urn:btih:{ih}&dn={dn}&{tr}"


def _get_lt_session() -> Any:
    global _lt_session, _monitor_started
    if not HAS_LIBTORRENT:
        return None
    with _session_lock:
        if _lt_session is None:
            _lt_session = lt.session({"listen_interfaces": "0.0.0.0:6881,[::]:6881"})
            try:
                settings = _lt_session.get_settings()
                settings["enable_dht"] = True
                settings["enable_lsd"] = True
                settings["enable_upnp"] = True
                settings["enable_natpmp"] = True
                settings["connections_limit"] = 250
                _lt_session.apply_settings(settings)
                _lt_session.add_dht_router("router.bittorrent.com", 6881)
                _lt_session.add_dht_router("router.utorrent.com", 6881)
                _lt_session.add_dht_router("dht.transmissionbt.com", 6881)
            except Exception:
                pass
        if not _monitor_started:
            _monitor_started = True
            t = threading.Thread(target=_monitor_loop, daemon=True)
            t.start()
        return _lt_session


def _monitor_loop() -> None:
    state_str = [
        "queued",
        "checking",
        "downloading metadata",
        "downloading",
        "finished",
        "seeding",
        "allocating",
        "checking fastresume",
    ]
    while True:
        time.sleep(1.0)
        with _session_lock:
            items = list(_downloads.items())
            ses = _lt_session
        if not ses:
            continue

        for tid, info in items:
            handle = info.get("handle")
            if not handle or info.get("status") in ("Cancelled", "Completed", "Error"):
                continue
            try:
                if not handle.is_valid():
                    continue
                s = handle.status()
                idx = int(s.state)
                raw_state = state_str[idx] if 0 <= idx < len(state_str) else "downloading"
                if info.get("paused"):
                    info["status"] = "Paused"
                else:
                    info["status"] = raw_state.title()
                info["progress"] = round(float(s.progress) * 100.0, 1)
                info["download_rate"] = round(float(s.download_rate) / 1024.0, 1)  # KB/s
                info["upload_rate"] = round(float(s.upload_rate) / 1024.0, 1)
                info["peers"] = int(s.num_peers)
                info["total_wanted"] = int(s.total_wanted)
                info["total_done"] = int(s.total_wanted_done)

                if s.is_seeding or s.state == lt.torrent_status.seeding or s.progress >= 1.0:
                    info["status"] = "Completed"
                    info["progress"] = 100.0
                    info["completed_at"] = int(time.time())
                    try:
                        ses.remove_torrent(handle)
                    except Exception:
                        pass
                    info["handle"] = None
            except Exception as e:
                info["status"] = "Error"
                info["error"] = str(e)


import concurrent.futures

CINEMETA_SEARCH_URL = "https://v3-cinemeta.strem.io/catalog/movie/top/search={query}.json"
TORRENTIO_STREAM_URL = (
    "https://torrentio.strem.fun/"
    "providers=yts,rarbg,1337x,thepiratebay,kickasstorrents,torrentgalaxy,magnetdl,rutor,rutracker/"
    "stream/movie/{imdb_id}.json"
)


def _parse_size_bytes(size_str: str) -> int:
    """Parse human-readable size strings like '50.86 GB' or '1.4 GB' or '850 MB' into bytes."""
    if not size_str:
        return 0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(TB|TiB|GB|GiB|MB|MiB|KB|KiB)", str(size_str), re.IGNORECASE)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2).upper()
    if unit in ("TB", "TIB"):
        return int(val * (1024 ** 4))
    if unit in ("GB", "GIB"):
        return int(val * (1024 ** 3))
    if unit in ("MB", "MIB"):
        return int(val * (1024 ** 2))
    if unit in ("KB", "KIB"):
        return int(val * 1024)
    return 0


def _format_size_human(size_bytes: int) -> str:
    if size_bytes <= 0:
        return ""
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024 ** 3):.2f} GB"
    return f"{size_bytes / (1024 ** 2):.0f} MB"


def _extract_info_hash(magnet_or_hash: str) -> str:
    raw = (magnet_or_hash or "").strip()
    m = re.search(r"xt=urn:btih:([a-fA-F0-9]{32,40})", raw)
    if m:
        return m.group(1).lower()
    if re.fullmatch(r"[a-fA-F0-9]{32,40}", raw):
        return raw.lower()
    return ""


# ─── Catalog & Release Search ─────────────────────────────────────────────────

def _annotate_release_candidate(
    title: str,
    release_name: str,
    quality: str,
    size_human: str,
    seeds: int,
    peers: int,
    magnet: str,
    source_provider: str,
    year: Optional[int] = None,
    poster_url: Optional[str] = None,
    imdb_id: str = "",
    rating: Optional[float] = None,
    size_bytes: int = 0,
) -> Dict[str, Any]:
    parsed = release_parser.parse_release_name(release_name or f"{title} {quality}")
    res = parsed.get("resolution") or quality or "1080p"
    is_4k = bool(
        parsed.get("is_4k")
        or "2160p" in (quality or "").lower()
        or "4k" in (quality or "").lower()
        or "2160p" in (release_name or "").lower()
        or "uhd" in (release_name or "").lower()
    )
    if is_4k and res not in ("2160p", "4K"):
        res = "2160p"
    is_remux = bool(
        parsed.get("is_remux")
        or "remux" in (release_name or "").lower()
        or "bdremux" in (release_name or "").lower()
    )
    hdr = parsed.get("hdr") or "SDR"
    vcodec = parsed.get("video_codec") or ("HEVC" if is_4k else "H.264")
    acodec = parsed.get("audio_codec") or ""
    warnings = list(parsed.get("warnings") or [])

    computed_bytes = int(size_bytes) if size_bytes and size_bytes > 0 else _parse_size_bytes(size_human)
    final_size_human = size_human or _format_size_human(computed_bytes)
    size_gb = round(computed_bytes / (1024 ** 3), 2) if computed_bytes > 0 else 0.0
    ih = _extract_info_hash(magnet)

    # Build a concise type tag for movie card buttons (e.g. "REMUX", "BluRay", "WEB")
    source_tag = parsed.get("source") or ""
    if is_remux:
        btn_type = "REMUX"
    elif source_tag:
        btn_type = source_tag
    else:
        btn_type = "WEB"

    return {
        "title": title or parsed.get("clean_title") or release_name,
        "year": year or parsed.get("year"),
        "release_name": release_name,
        "quality": res,
        "type": btn_type,
        "size": final_size_human,
        "size_bytes": computed_bytes,
        "size_gb": size_gb,
        "seeds": int(seeds or 0),
        "peers": int(peers or 0),
        "magnet": magnet,
        "hash": ih,
        "provider": source_provider,
        "poster_url": poster_url,
        "imdb_id": imdb_id,
        "rating": rating,
        "is_4k": is_4k,
        "is_remux": is_remux,
        "hdr": hdr,
        "video_codec": vcodec,
        "audio_codec": acodec,
        "group": parsed.get("group") or "",
        "pi5_compatible": parsed.get("pi5_compatible", True),
        "warnings": warnings,
    }


def search_cinemeta(query: str) -> List[Dict[str, Any]]:
    """Query Stremio Cinemeta catalog to resolve movie titles to IMDb IDs, posters, and years."""
    q = (query or "").strip()
    if not q:
        return []
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) stream-pi/2.0"}
    try:
        url = CINEMETA_SEARCH_URL.format(query=urllib.parse.quote(q))
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code != 200:
            return []
        metas = (resp.json() or {}).get("metas") or []
        out: List[Dict[str, Any]] = []
        for m in metas[:8]:
            imdb_id = m.get("imdb_id") or m.get("id") or ""
            if not imdb_id.startswith("tt"):
                continue
            title = m.get("name") or ""
            year_raw = str(m.get("releaseInfo") or m.get("year") or "")
            year_m = re.match(r"(\d{4})", year_raw)
            year = int(year_m.group(1)) if year_m else None
            poster = m.get("poster") or ""
            genres = m.get("genre") or m.get("genres") or []
            rating_raw = m.get("imdbRating")
            try:
                rating = float(rating_raw) if rating_raw else None
            except ValueError:
                rating = None
            out.append({
                "name": title,
                "title": title,
                "year": year,
                "rating": rating or "N/A",
                "poster": poster,
                "cover": poster,
                "genre": genres,
                "genres": genres,
                "imdb_id": imdb_id,
                "torrents": [],
            })
        return out
    except Exception:
        return []


def search_torrentio_by_imdb(
    imdb_id: str,
    title: str = "",
    year: Optional[int] = None,
    poster_url: str = "",
    rating: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Query Torrentio multi-indexer (RARBG, 1337x, TorrentGalaxy, TPB, RuTracker, YTS) by IMDb ID."""
    if not imdb_id or not imdb_id.startswith("tt"):
        return []
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) stream-pi/2.0"}
    try:
        url = TORRENTIO_STREAM_URL.format(imdb_id=imdb_id)
        resp = requests.get(url, headers=headers, timeout=7)
        if resp.status_code != 200:
            return []
        streams = (resp.json() or {}).get("streams") or []
        results: List[Dict[str, Any]] = []
        for st in streams:
            ih = (st.get("infoHash") or "").strip().lower()
            if not ih:
                continue
            raw_title = st.get("title") or ""
            lines = [ln.strip() for ln in raw_title.splitlines() if ln.strip()]
            release_name = lines[0] if lines else (st.get("behaviorHints", {}).get("filename") or title)
            meta_line = lines[-1] if len(lines) > 1 else ""

            # Extract seeders (👤 61), size (💾 50.86 GB), and indexer (⚙️ RARBG / TorrentGalaxy / 1337x)
            seeds_m = re.search(r"👤\s*(\d+)", raw_title)
            seeds = int(seeds_m.group(1)) if seeds_m else 0

            size_m = re.search(r"💾\s*([\d.]+\s*(?:TB|GB|MB|KB))", raw_title, re.IGNORECASE)
            size_human = size_m.group(1) if size_m else ""
            size_bytes = int((st.get("behaviorHints") or {}).get("videoSize") or 0)
            if not size_bytes and size_human:
                size_bytes = _parse_size_bytes(size_human)
            if not size_human and size_bytes > 0:
                size_human = _format_size_human(size_bytes)

            prov_m = re.search(r"⚙️\s*([^\n\r]+)", raw_title)
            provider = prov_m.group(1).strip() if prov_m else "Torrentio"

            name_col = st.get("name") or ""
            qual_m = re.search(r"(4k|2160p|1080p|720p|480p)", name_col + " " + release_name, re.IGNORECASE)
            qual = qual_m.group(1).lower() if qual_m else "1080p"
            if qual == "4k":
                qual = "2160p"

            # Build magnet including extra trackers provided by Torrentio
            extra_trs = []
            for src in (st.get("sources") or []):
                if isinstance(src, str) and src.startswith("tracker:"):
                    extra_trs.append(src[len("tracker:"):])
            all_trackers = list(dict.fromkeys(extra_trs + TRACKERS))
            dn = urllib.parse.quote_plus(release_name or title or "Movie")
            tr_str = "&".join(f"tr={urllib.parse.quote_plus(t)}" for t in all_trackers[:12])
            magnet = f"magnet:?xt=urn:btih:{ih}&dn={dn}&{tr_str}"

            cand = _annotate_release_candidate(
                title=title,
                release_name=release_name,
                quality=qual,
                size_human=size_human,
                seeds=seeds,
                peers=0,
                magnet=magnet,
                source_provider=provider,
                year=year,
                poster_url=poster_url,
                imdb_id=imdb_id,
                rating=rating,
                size_bytes=size_bytes,
            )
            results.append(cand)
        return results
    except Exception:
        return []


def search_yts(query: str) -> List[Dict[str, Any]]:
    """Search YTS movie catalog (returns legacy-compatible list enriched with 4K/HDR metadata)."""
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) stream-pi/2.0"}
    for domain in YTS_DOMAINS:
        try:
            url = f"{domain}/api/v2/list_movies.json?query_term={urllib.parse.quote(query)}&limit=15"
            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code != 200:
                continue
            data = res.json()
            movies = (data.get("data") or {}).get("movies") or []
            results: List[Dict[str, Any]] = []
            for m in movies:
                title = m.get("title") or ""
                year = m.get("year")
                torrents = m.get("torrents") or []
                enriched_torrents = []
                for t in torrents:
                    thash = t.get("hash")
                    if not thash:
                        continue
                    qual = t.get("quality") or "1080p"
                    ttype = (t.get("type") or "web").upper()
                    vcodec = t.get("video_codec") or ("x265" if qual == "2160p" else "x264")
                    bit_depth = t.get("bit_depth")
                    rel_title = f"{title}.{year}.{qual}.{ttype}.{vcodec}{'.10bit' if str(bit_depth) == '10' else ''}-YTS"
                    magnet = _build_magnet(thash, f"{title} ({year}) [{qual}]")
                    size_b = int(t.get("size_bytes") or 0)
                    cand = _annotate_release_candidate(
                        title=title,
                        release_name=rel_title,
                        quality=qual,
                        size_human=t.get("size") or "",
                        seeds=int(t.get("seeds") or 0),
                        peers=int(t.get("peers") or 0),
                        magnet=magnet,
                        source_provider="YTS",
                        year=year,
                        poster_url=m.get("medium_cover_image") or m.get("small_cover_image"),
                        imdb_id=m.get("imdb_code") or "",
                        rating=m.get("rating"),
                        size_bytes=size_b,
                    )
                    cand["hash"] = thash.lower()
                    cand["type"] = t.get("type") or "web"
                    enriched_torrents.append(cand)

                if enriched_torrents:
                    poster_img = m.get("medium_cover_image") or m.get("small_cover_image") or ""
                    genres = m.get("genres") or []
                    results.append({
                        "name": title,
                        "title": title,
                        "year": year,
                        "rating": m.get("rating") or "N/A",
                        "poster": poster_img,
                        "cover": poster_img,
                        "genre": genres,
                        "genres": genres,
                        "imdb_id": m.get("imdb_code") or "",
                        "torrents": enriched_torrents,
                    })
            return results
        except Exception:
            continue
    return []


def search_remux_and_uhd(query: str, remux_only: bool = False) -> List[Dict[str, Any]]:
    """Search public indexers (or configured Torznab URL) for 4K UHD Blu-ray Remuxes & high-bitrate releases."""
    q = (query or "").strip()
    if not q:
        return []

    search_term = f"{q} 2160p remux" if remux_only else q
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) stream-pi/2.0"}
    candidates: List[Dict[str, Any]] = []

    # 1. Optional custom Torznab endpoint (e.g. Prowlarr / Jackett on LAN)
    torznab_url = os.environ.get("TORZNAB_URL", "").strip()
    torznab_key = os.environ.get("TORZNAB_API_KEY", "").strip()
    if torznab_url:
        try:
            resp = requests.get(
                torznab_url,
                params={"t": "search", "q": search_term, "apikey": torznab_key, "o": "json"},
                headers=headers,
                timeout=7,
            )
            if resp.status_code == 200:
                items = (resp.json().get("channel") or {}).get("item") or []
                if isinstance(items, dict):
                    items = [items]
                for it in items[:100]:
                    rel_name = it.get("title") or ""
                    magnet_or_link = it.get("link") or it.get("enclosure", {}).get("@url") or ""
                    size_b = int(it.get("size") or 0)
                    size_h = _format_size_human(size_b)
                    parsed = release_parser.parse_release_name(rel_name)
                    candidates.append(_annotate_release_candidate(
                        title=parsed.get("clean_title") or rel_name,
                        release_name=rel_name,
                        quality=parsed.get("resolution") or "2160p",
                        size_human=size_h,
                        seeds=int(it.get("seeders") or 0),
                        peers=int(it.get("peers") or 0),
                        magnet=magnet_or_link,
                        source_provider="Torznab",
                        year=parsed.get("year"),
                        size_bytes=size_b,
                    ))
        except Exception:
            pass

    # 2. Public APIBay indexer (scan all up to 100 rows so large Remuxes & 2160p releases are never cut off)
    try:
        resp = requests.get(
            APIBAY_URL,
            params={"q": search_term, "cat": "207,201"},
            headers=headers,
            timeout=6,
        )
        if resp.status_code == 200:
            rows = resp.json()
            if isinstance(rows, list):
                for row in rows[:100]:
                    ih = row.get("info_hash") or ""
                    name = row.get("name") or ""
                    if not ih or ih == "0" * 40 or name == "No results returned":
                        continue
                    size_b = int(row.get("size") or 0)
                    size_h = _format_size_human(size_b)
                    parsed = release_parser.parse_release_name(name)
                    if remux_only and not parsed.get("is_remux") and "remux" not in name.lower():
                        continue
                    magnet = _build_magnet(ih, name)
                    candidates.append(_annotate_release_candidate(
                        title=parsed.get("clean_title") or name,
                        release_name=name,
                        quality=parsed.get("resolution") or "1080p",
                        size_human=size_h,
                        seeds=int(row.get("seeders") or 0),
                        peers=int(row.get("leechers") or 0),
                        magnet=magnet,
                        source_provider="TPB/APIBay",
                        year=parsed.get("year"),
                        size_bytes=size_b,
                    ))
    except Exception:
        pass

    candidates.sort(
        key=lambda c: (1 if c.get("is_remux") else 0, 1 if c.get("is_4k") else 0, c.get("seeds", 0), c.get("size_bytes", 0)),
        reverse=True,
    )
    return candidates


def filter_and_sort_releases(
    releases: List[Dict[str, Any]],
    min_gb: Optional[float] = None,
    max_gb: Optional[float] = None,
    resolution: Optional[str] = None,
    sort_by: str = "seeds",
) -> List[Dict[str, Any]]:
    """Filter release candidates by min/max size (GB) and resolution/remux, then sort."""
    res_filter = (resolution or "").strip().lower()
    filtered: List[Dict[str, Any]] = []

    for r in releases:
        size_gb = float(r.get("size_gb") or 0.0)
        if min_gb is not None and min_gb > 0 and size_gb < min_gb:
            continue
        if max_gb is not None and max_gb > 0 and size_gb > max_gb:
            continue

        if res_filter and res_filter != "any":
            qual_l = (r.get("quality") or "").lower()
            if res_filter == "remux":
                if not r.get("is_remux"):
                    continue
            elif res_filter in ("2160p", "4k", "uhd"):
                if not r.get("is_4k") and "2160p" not in qual_l:
                    continue
            elif res_filter == "1080p":
                if "1080p" not in qual_l or r.get("is_4k"):
                    continue
            elif res_filter == "720p":
                if "720p" not in qual_l:
                    continue

        filtered.append(r)

    s_mode = (sort_by or "seeds").strip().lower()
    if s_mode == "largest":
        filtered.sort(key=lambda c: (int(c.get("size_bytes") or 0), int(c.get("seeds") or 0)), reverse=True)
    elif s_mode == "smallest":
        filtered.sort(
            key=lambda c: (
                int(c.get("size_bytes") or 0) if int(c.get("size_bytes") or 0) > 0 else 10 ** 15,
                -int(c.get("seeds") or 0),
            )
        )
    elif s_mode == "remux":
        filtered.sort(
            key=lambda c: (1 if c.get("is_remux") else 0, 1 if c.get("is_4k") else 0, int(c.get("seeds") or 0), int(c.get("size_bytes") or 0)),
            reverse=True,
        )
    else:  # default: most seeders
        filtered.sort(key=lambda c: (int(c.get("seeds") or 0), int(c.get("size_bytes") or 0)), reverse=True)

    return filtered


def search_all_sources(
    query: str,
    mode: str = "all",
    min_gb: Optional[float] = None,
    max_gb: Optional[float] = None,
    resolution: Optional[str] = None,
    sort_by: str = "seeds",
) -> Dict[str, Any]:
    """Unified search across YTS, Cinemeta + Torrentio (RARBG/1337x/TorrentGalaxy/RuTracker), and APIBay."""
    m = (mode or "all").lower()
    if m == "remux" and not resolution:
        resolution = "remux"
    elif m in ("4k", "uhd") and not resolution:
        resolution = "2160p"

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        fut_yts = pool.submit(search_yts, query) if m in ("all", "yts", "standard") else None
        fut_cine = pool.submit(search_cinemeta, query)
        fut_apibay = pool.submit(search_remux_and_uhd, query, remux_only=(m == "remux")) if m in ("all", "remux", "uhd", "4k") else None

        yts_movies = fut_yts.result() if fut_yts else []
        cine_movies = fut_cine.result()
        apibay_items = fut_apibay.result() if fut_apibay else []

    # Merge movie cards by IMDb ID or normalized (title, year)
    movie_map: Dict[str, Dict[str, Any]] = {}
    movie_order: List[str] = []

    for mv in yts_movies + cine_movies:
        imdb_id = (mv.get("imdb_id") or "").strip()
        norm_key = imdb_id if imdb_id else f"{(mv.get('title') or '').lower().strip()}:{mv.get('year') or ''}"
        if not norm_key:
            continue
        if norm_key not in movie_map:
            movie_map[norm_key] = dict(mv)
            movie_map[norm_key]["torrents"] = list(mv.get("torrents") or [])
            movie_order.append(norm_key)
        else:
            existing = movie_map[norm_key]
            if not existing.get("poster") and mv.get("poster"):
                existing["poster"] = mv["poster"]
                existing["cover"] = mv["poster"]
            if (not existing.get("genre") or existing.get("genre") == []) and mv.get("genre"):
                existing["genre"] = mv["genre"]
                existing["genres"] = mv["genre"]
            if existing.get("rating") in (None, "", "N/A") and mv.get("rating") not in (None, "", "N/A"):
                existing["rating"] = mv["rating"]
            existing["torrents"].extend(mv.get("torrents") or [])

    # Query Torrentio for up to the top 3 matched IMDb IDs in parallel
    imdb_keys = [k for k in movie_order if k.startswith("tt")][:3]
    torrentio_by_key: Dict[str, List[Dict[str, Any]]] = {}
    if imdb_keys:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futs = {
                k: pool.submit(
                    search_torrentio_by_imdb,
                    k,
                    title=movie_map[k].get("title") or "",
                    year=movie_map[k].get("year"),
                    poster_url=movie_map[k].get("poster") or "",
                    rating=movie_map[k].get("rating") if isinstance(movie_map[k].get("rating"), (int, float)) else None,
                )
                for k in imdb_keys
            }
            for k, f in futs.items():
                try:
                    torrentio_by_key[k] = f.result()
                except Exception:
                    torrentio_by_key[k] = []

    all_releases_by_hash: Dict[str, Dict[str, Any]] = {}

    # Attach Torrentio streams to their movie card AND to the unified release list
    for k, t_streams in torrentio_by_key.items():
        mv = movie_map[k]
        seen_hashes = {_extract_info_hash(t.get("magnet") or t.get("hash") or "") for t in mv["torrents"]}
        for st in t_streams:
            ih = st.get("hash") or _extract_info_hash(st.get("magnet") or "")
            if ih and ih not in seen_hashes:
                seen_hashes.add(ih)
                mv["torrents"].append(st)
            if ih and ih not in all_releases_by_hash:
                all_releases_by_hash[ih] = st

    # Include APIBay / Torznab items in all_releases_by_hash and attach to matching movie cards
    for item in apibay_items:
        ih = item.get("hash") or _extract_info_hash(item.get("magnet") or "")
        key = ih or item.get("release_name") or ""
        if key and key not in all_releases_by_hash:
            all_releases_by_hash[key] = item
        item_title_l = (item.get("title") or "").lower().strip()
        for k in movie_order:
            mv = movie_map[k]
            mv_title_l = (mv.get("title") or "").lower().strip()
            if mv_title_l and item_title_l == mv_title_l:
                if mv.get("year") and item.get("year") and int(mv["year"]) != int(item["year"]):
                    continue
                seen_hashes = {_extract_info_hash(t.get("magnet") or t.get("hash") or "") for t in mv["torrents"]}
                if ih and ih not in seen_hashes:
                    item_copy = dict(item)
                    item_copy["poster_url"] = mv.get("poster") or ""
                    mv["torrents"].append(item_copy)

    # Also include YTS torrents in all_releases_by_hash so sorting/filtering works uniformly
    for k in movie_order:
        for t in movie_map[k].get("torrents") or []:
            ih = t.get("hash") or _extract_info_hash(t.get("magnet") or "")
            if ih and ih not in all_releases_by_hash:
                all_releases_by_hash[ih] = t

    # Filter and sort each movie's torrents and build final movies list
    final_movies: List[Dict[str, Any]] = []
    for k in movie_order:
        mv = movie_map[k]
        filtered_torrents = filter_and_sort_releases(
            mv.get("torrents") or [],
            min_gb=min_gb,
            max_gb=max_gb,
            resolution=resolution,
            sort_by=sort_by,
        )
        if not filtered_torrents:
            continue
        mv_copy = dict(mv)
        mv_copy["torrents"] = filtered_torrents
        final_movies.append(mv_copy)

    # Filter and sort all standalone releases
    final_releases = filter_and_sort_releases(
        list(all_releases_by_hash.values()),
        min_gb=min_gb,
        max_gb=max_gb,
        resolution=resolution,
        sort_by=sort_by,
    )

    return {
        "query": query,
        "mode": m,
        "movies": final_movies,
        "releases": final_releases,
        "has_libtorrent": HAS_LIBTORRENT,
        "storage_warning": config.storage_problem(),
    }



# ─── Download Lifecycle Management ────────────────────────────────────────────

def start_download(
    info_hash_or_magnet: str,
    title: str,
    save_root: Optional[str] = None,
    poster_url: str = "",
) -> Tuple[bool, str, Optional[str]]:
    """Start downloading a movie via magnet URI or 40-char info_hash into MOVIES_DIR.

    Returns `(ok, message_or_error, download_id)`.
    """
    prob = config.storage_problem()
    if prob:
        return False, prob, None

    raw = (info_hash_or_magnet or "").strip()
    if not raw:
        return False, "Missing magnet URI or info_hash", None

    # Extract canonical ID
    if raw.startswith("magnet:?"):
        magnet_uri = raw
        m = re.search(r"xt=urn:btih:([a-fA-F0-9]{32,40})", raw)
        tid = m.group(1).lower() if m else f"dl-{int(time.time())}"
        if not title:
            dn_m = re.search(r"[?&]dn=([^&]+)", raw)
            if dn_m:
                title = urllib.parse.unquote_plus(dn_m.group(1))
    else:
        tid = re.sub(r"[^a-fA-F0-9]", "", raw).lower() or f"dl-{int(time.time())}"
        magnet_uri = _build_magnet(raw, title or tid)

    with _session_lock:
        existing = _downloads.get(tid)
        if existing and existing.get("status") not in ("Cancelled", "Completed", "Error"):
            return False, "Download already in progress", tid

    root_dir = save_root or config.MOVIES_DIR
    folder_name = _sanitize_folder_name(title or tid)
    save_path = os.path.join(root_dir, folder_name)
    os.makedirs(save_path, exist_ok=True)

    if poster_url:
        def _save_poster_bg(url: str, dest: str) -> None:
            try:
                r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                if r.status_code == 200 and r.content:
                    with open(dest, "wb") as f:
                        f.write(r.content)
            except Exception:
                pass
        threading.Thread(
            target=_save_poster_bg,
            args=(poster_url, os.path.join(save_path, f"{folder_name}.jpg")),
            daemon=True,
        ).start()

    if not HAS_LIBTORRENT:
        return False, "python3-libtorrent is not installed in this environment", tid

    ses = _get_lt_session()
    try:
        if hasattr(lt, "parse_magnet_uri"):
            params = lt.parse_magnet_uri(magnet_uri)
            params.save_path = save_path
            params.storage_mode = lt.storage_mode_t.storage_mode_sparse
            handle = ses.add_torrent(params)
        else:
            params = {
                "save_path": save_path,
                "storage_mode": lt.storage_mode_t.storage_mode_sparse,
            }
            handle = lt.add_magnet_uri(ses, magnet_uri, params)
    except Exception as e:
        return False, f"Failed to add magnet to libtorrent: {e}", tid

    with _session_lock:
        _downloads[tid] = {
            "id": tid,
            "title": title or folder_name,
            "folder": folder_name,
            "progress": 0.0,
            "status": "Starting...",
            "download_rate": 0.0,
            "upload_rate": 0.0,
            "peers": 0,
            "total_wanted": 0,
            "total_done": 0,
            "paused": False,
            "handle": handle,
            "save_path": save_path,
            "started_at": int(time.time()),
        }
    return True, "Download started", tid


def pause_download(tid: str) -> bool:
    with _session_lock:
        info = _downloads.get(tid)
        if not info or not info.get("handle"):
            return False
        try:
            info["handle"].pause()
            info["paused"] = True
            info["status"] = "Paused"
            return True
        except Exception:
            return False


def resume_download(tid: str) -> bool:
    with _session_lock:
        info = _downloads.get(tid)
        if not info or not info.get("handle"):
            return False
        try:
            info["handle"].resume()
            info["paused"] = False
            info["status"] = "Downloading"
            return True
        except Exception:
            return False


def cancel_download(tid: str, delete_files: bool = True) -> bool:
    with _session_lock:
        info = _downloads.get(tid)
        if not info:
            return False
        handle = info.get("handle")
        if handle and _lt_session:
            try:
                _lt_session.remove_torrent(handle)
            except Exception:
                pass
        info["handle"] = None
        info["status"] = "Cancelled"
        save_path = info.get("save_path")

    if delete_files and save_path and os.path.isdir(save_path):
        root_real = os.path.realpath(config.MOVIES_DIR)
        target_real = os.path.realpath(save_path)
        if target_real.startswith(root_real + os.sep):
            shutil.rmtree(target_real, ignore_errors=True)
    return True


def clear_finished() -> int:
    removed = 0
    with _session_lock:
        for k in list(_downloads.keys()):
            if _downloads[k].get("status") in ("Completed", "Cancelled", "Error"):
                del _downloads[k]
                removed += 1
    return removed


def get_download_status() -> Dict[str, Dict[str, Any]]:
    """Return serializable status dictionary of all downloads."""
    with _session_lock:
        clean: Dict[str, Dict[str, Any]] = {}
        for k, v in _downloads.items():
            rate_kbps = float(v.get("download_rate", 0.0) or 0.0)
            st_label = v.get("status", "Unknown")
            peers_cnt = int(v.get("peers", 0) or 0)
            clean[k] = {
                "id": k,
                "title": v.get("title"),
                "folder": v.get("folder"),
                "progress": v.get("progress", 0.0),
                "status": st_label,
                "state": st_label.lower(),
                "download_rate": round(rate_kbps * 1024.0, 1),
                "download_rate_kbps": rate_kbps,
                "upload_rate": v.get("upload_rate", 0.0),
                "peers": peers_cnt,
                "num_peers": peers_cnt,
                "total_wanted": v.get("total_wanted", 0),
                "total_done": v.get("total_done", 0),
                "paused": v.get("paused", False),
                "error": v.get("error"),
            }
        return clean
