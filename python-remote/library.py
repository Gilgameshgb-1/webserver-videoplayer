"""Movie Library Scanner, Capability Indexer, and Progress Store for stream.pi.

Responsibilities:
- Discovers movies across MOVIES_DIR and EXTRA_MOVIES_DIRS (ignoring 'anime', etc.)
- Combines ffprobe stream analysis (media_probe), subtitle discovery & provenance
  (subtitles), release filename parsing (release_parser), and movie metadata (metadata)
- Computes rich capability indicators:
    * 4K UHD / 1080p resolution
    * HDR10 / HDR10+ / Dolby Vision / HLG
    * Blu-ray Remux indicator
    * HR subtitles (distinguishing local embedded/sidecar vs downloadable online,
      plus release synchronization confidence)
    * EN subtitles
    * Multiple audio tracks & primary codec badge (TrueHD Atmos, DTS-HD MA, etc.)
- Supports library filtering (hr_local, hr_downloadable, hr_any, en_local, 4k, hdr,
  remux, multi_audio, watching, unwatched) and sorting
- Persists playback resume positions and per-movie subtitle delay / track preferences
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import config
import media_probe
import metadata
import release_parser
import subtitles

_resume_lock = threading.Lock()
_online_sub_lock = threading.Lock()

ONLINE_SUB_CACHE_FILE = os.path.join(config.STATE_DIR, "online-sub-availability.json")
SAMPLE_RE = re.compile(r"(?:^|[.\-_ ()\[\]])(sample|trailer|featurette|extras?)(?:$|[.\-_ ()\[\]])", re.IGNORECASE)


def _atomic_save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


# ─── Resume & Per-Movie Preferences Store ─────────────────────────────────────

def load_resume_store() -> Dict[str, Any]:
    """Load playback positions and per-movie track/delay preferences."""
    with _resume_lock:
        data: Dict[str, Any] = {}
        # Migrate legacy resume store if present
        if os.path.isfile(config.LEGACY_RESUME_FILE) and not os.path.isfile(config.RESUME_FILE):
            try:
                with open(config.LEGACY_RESUME_FILE, "r", encoding="utf-8") as f:
                    legacy = json.load(f)
                if isinstance(legacy, dict):
                    for k, v in legacy.items():
                        if isinstance(v, (int, float)):
                            data[k] = {"position": float(v), "updated_at": int(time.time())}
                        elif isinstance(v, dict):
                            data[k] = v
                    _atomic_save_json(config.RESUME_FILE, data)
            except Exception:
                pass

        if os.path.isfile(config.RESUME_FILE):
            try:
                with open(config.RESUME_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if isinstance(v, (int, float)):
                            data[k] = {"position": float(v)}
                        elif isinstance(v, dict):
                            data[k] = v
            except Exception:
                pass
        return data


def get_resume_entry(key: str, fallback_keys: Optional[List[str]] = None) -> Dict[str, Any]:
    store = load_resume_store()
    if key in store:
        return store[key]
    for fk in (fallback_keys or []):
        if fk in store:
            return store[fk]
    return {}


def update_resume_entry(
    key: str,
    position: Optional[float] = None,
    duration: Optional[float] = None,
    sub_delay: Optional[float] = None,
    sid: Optional[Any] = None,
    aid: Optional[Any] = None,
    audio_mode: Optional[str] = None,
) -> Dict[str, Any]:
    store = load_resume_store()
    with _resume_lock:
        entry = dict(store.get(key) or {})
        if position is not None:
            entry["position"] = round(max(0.0, float(position)), 2)
        if duration is not None and duration > 0:
            entry["duration"] = round(float(duration), 2)
        if entry.get("position") is not None and entry.get("duration"):
            pct = (float(entry["position"]) / float(entry["duration"])) * 100.0
            entry["percent"] = round(min(100.0, max(0.0, pct)), 1)
        if sub_delay is not None:
            entry["sub_delay"] = round(float(sub_delay), 3)
        if sid is not None:
            entry["sid"] = sid
        if aid is not None:
            entry["aid"] = aid
        if audio_mode is not None:
            entry["audio_mode"] = audio_mode
        entry["updated_at"] = int(time.time())
        store[key] = entry
        _atomic_save_json(config.RESUME_FILE, store)
        # Also mirror simple position map to legacy file if it exists
        if os.path.isfile(config.LEGACY_RESUME_FILE):
            try:
                simple = {k: v.get("position", 0) for k, v in store.items() if isinstance(v, dict)}
                _atomic_save_json(config.LEGACY_RESUME_FILE, simple)
            except Exception:
                pass
        return entry


# ─── Online Subtitle Availability Cache ───────────────────────────────────────

def _load_online_sub_cache() -> Dict[str, Any]:
    with _online_sub_lock:
        if not os.path.isfile(ONLINE_SUB_CACHE_FILE):
            return {}
        try:
            with open(ONLINE_SUB_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def get_online_sub_status(rel_video: str) -> Dict[str, Any]:
    """Return cached online Croatian subtitle availability for a movie, if checked."""
    cache = _load_online_sub_cache()
    entry = cache.get(rel_video)
    if isinstance(entry, dict):
        return entry
    return {
        "checked": False,
        "hr_available_online": None,  # Unknown until checked, though providers are available
        "hr_online_count": 0,
        "hr_best_sync": None,
        "en_available_online": None,
        "en_online_count": 0,
        "providers_ready": True,  # Podnapisi works keylessly; OpenSubtitles/SubDL optional
    }


def check_online_subtitles_for_movie(
    video_path: str,
    rel_video: str,
    title: str = "",
    year: Optional[int] = None,
    imdb_id: str = "",
    tmdb_id: Optional[int] = None,
    languages: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Query subtitle providers and persist availability + sync confidence for library filtering."""
    langs = languages or ["hr", "en"]
    res = subtitles.search_online_subtitles(
        video_path=video_path,
        title=title,
        year=year,
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        languages=langs,
    )
    results = res.get("results", [])
    hr_results = [r for r in results if r.get("lang") == "hr"]
    en_results = [r for r in results if r.get("lang") == "en"]

    sync_rank = {"exact": 4, "likely": 3, "unverified": 2, "fps_mismatch": 1}
    hr_best_sync = None
    if hr_results:
        best = max(hr_results, key=lambda r: sync_rank.get(r.get("sync_confidence", "unverified"), 0))
        hr_best_sync = best.get("sync_confidence", "unverified")

    status = {
        "checked": True,
        "checked_at": int(time.time()),
        "hr_available_online": len(hr_results) > 0,
        "hr_online_count": len(hr_results),
        "hr_best_sync": hr_best_sync,
        "en_available_online": len(en_results) > 0,
        "en_online_count": len(en_results),
        "providers_ready": True,
        "providers": res.get("providers", []),
    }
    cache = _load_online_sub_cache()
    with _online_sub_lock:
        cache[rel_video] = status
        _atomic_save_json(ONLINE_SUB_CACHE_FILE, cache)
    return {
        "status": status,
        "search_response": res,
    }


# ─── Directory Discovery ──────────────────────────────────────────────────────

def _make_movie_id(rel_video: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", rel_video.strip("/")).strip("-").lower()
    short_hash = hashlib.sha1(rel_video.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:64]}-{short_hash}"


def _discover_video_items() -> List[Dict[str, Any]]:
    """Find all movie video files across configured roots."""
    roots = [config.MOVIES_DIR] + [
        d for d in config.EXTRA_MOVIES_DIRS if os.path.abspath(d) != os.path.abspath(config.MOVIES_DIR)
    ]
    items: List[Dict[str, Any]] = []
    seen_realpaths = set()

    for root_idx, root in enumerate(roots):
        if not os.path.isdir(root):
            continue
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue

        for entry in entries:
            if entry.startswith(".") or entry in config.IGNORED_DIRS:
                continue
            full_entry = os.path.join(root, entry)

            # Case 1: Loose video file directly inside MOVIES_DIR
            if os.path.isfile(full_entry):
                ext = os.path.splitext(entry)[1].lower()
                if ext in config.VIDEO_EXTS:
                    rp = os.path.realpath(full_entry)
                    if rp in seen_realpaths:
                        continue
                    seen_realpaths.add(rp)
                    items.append({
                        "root": root,
                        "is_primary_root": root_idx == 0,
                        "folder": "",
                        "display_folder": os.path.splitext(entry)[0],
                        "folder_path": root,
                        "video_filename": entry,
                        "video_path": full_entry,
                        "rel_video": entry,
                        "extra_videos": [],
                    })
                continue

            # Case 2: Movie subdirectory
            if os.path.isdir(full_entry):
                video_candidates: List[Tuple[str, str, int, bool]] = []
                for walk_root, dirs, files in os.walk(full_entry):
                    dirs[:] = [d for d in dirs if not d.startswith(".") and d not in config.IGNORED_DIRS]
                    rel_depth = os.path.relpath(walk_root, full_entry)
                    if rel_depth != "." and rel_depth.count(os.sep) >= 2:
                        continue
                    for fname in sorted(files):
                        if fname.startswith("."):
                            continue
                        ext = os.path.splitext(fname)[1].lower()
                        if ext in config.VIDEO_EXTS:
                            fpath = os.path.join(walk_root, fname)
                            try:
                                sz = os.path.getsize(fpath)
                            except OSError:
                                sz = 0
                            is_sample = bool(SAMPLE_RE.search(fname)) and sz < 150 * 1024 * 1024
                            rel_in_folder = os.path.relpath(fpath, full_entry)
                            video_candidates.append((fpath, rel_in_folder, sz, is_sample))

                if not video_candidates:
                    continue

                # Prefer non-sample files, largest first
                non_samples = [c for c in video_candidates if not c[3]]
                pool = non_samples if non_samples else video_candidates
                pool.sort(key=lambda c: (-c[2], c[1]))
                primary_path, primary_rel_in_folder, _, _ = pool[0]

                rp = os.path.realpath(primary_path)
                if rp in seen_realpaths:
                    continue
                seen_realpaths.add(rp)

                rel_video = os.path.relpath(primary_path, root)
                extra_videos = [
                    {
                        "filename": c[1],
                        "rel_video": os.path.relpath(c[0], root),
                        "size_bytes": c[2],
                        "is_sample": c[3],
                    }
                    for c in video_candidates
                    if c[0] != primary_path
                ]

                items.append({
                    "root": root,
                    "is_primary_root": root_idx == 0,
                    "folder": entry,
                    "display_folder": entry,
                    "folder_path": full_entry,
                    "video_filename": primary_rel_in_folder,
                    "video_path": primary_path,
                    "rel_video": rel_video,
                    "extra_videos": extra_videos,
                })

    return items


# ─── Movie Record Builder ─────────────────────────────────────────────────────

def _build_badges(
    probe: Dict[str, Any],
    rel_info: Dict[str, Any],
    sub_summary: Dict[str, Any],
    online_sub: Dict[str, Any],
) -> List[Dict[str, str]]:
    badges: List[Dict[str, str]] = []

    # 1. Resolution badge
    res_label = probe.get("resolution_label") or rel_info.get("resolution") or ""
    if probe.get("is_4k") or res_label in ("4K UHD", "2160p"):
        badges.append({"id": "4k", "label": "4K UHD", "category": "video", "tone": "gold"})
    elif res_label == "1080p":
        badges.append({"id": "1080p", "label": "1080p", "category": "video", "tone": "neutral"})
    elif res_label:
        badges.append({"id": "res", "label": res_label, "category": "video", "tone": "neutral"})

    # 2. HDR badge
    hdr_type = probe.get("hdr_type") or rel_info.get("hdr")
    if hdr_type and hdr_type != "SDR":
        badges.append({"id": "hdr", "label": hdr_type, "category": "hdr", "tone": "amber"})

    # 3. Remux / Source badge
    is_remux = bool(probe.get("is_remux") or rel_info.get("is_remux"))
    if is_remux:
        badges.append({"id": "remux", "label": "Blu-ray Remux", "category": "source", "tone": "purple"})
    elif rel_info.get("source"):
        badges.append({"id": "source", "label": rel_info["source"], "category": "source", "tone": "neutral"})

    # 4. Croatian subtitles badge (local vs downloadable)
    if sub_summary.get("has_hr_local"):
        best_sync = sub_summary.get("best_hr_sync") or "unverified"
        sync_labels = {
            "embedded": "Embedded",
            "exact": "Exact Sync",
            "likely": "Likely Sync",
            "unverified": "Local",
            "fps_mismatch": "FPS Warning",
        }
        detail = sync_labels.get(best_sync, "Local")
        badges.append({
            "id": "hr_local",
            "label": "HR subtitles",
            "detail": detail,
            "category": "subtitle_hr",
            "tone": "emerald" if best_sync != "fps_mismatch" else "amber",
        })
    elif online_sub.get("hr_available_online") is True:
        cnt = online_sub.get("hr_online_count", 0)
        badges.append({
            "id": "hr_online",
            "label": "HR downloadable",
            "detail": f"{cnt} online" if cnt else "Online",
            "category": "subtitle_hr",
            "tone": "cyan",
        })

    # 5. English subtitles badge
    if sub_summary.get("has_en_local"):
        badges.append({
            "id": "en_local",
            "label": "EN subtitles",
            "detail": "Local",
            "category": "subtitle_en",
            "tone": "blue",
        })

    # 6. Multiple audio tracks badge
    audio_tracks = probe.get("audio_tracks") or []
    if len(audio_tracks) > 1:
        badges.append({
            "id": "multi_audio",
            "label": "Multiple audio tracks",
            "detail": f"{len(audio_tracks)} tracks",
            "category": "audio",
            "tone": "indigo",
        })
    elif len(audio_tracks) == 1:
        a0 = audio_tracks[0]
        codec_lbl = a0.get("codec_label") or ""
        ch_lbl = a0.get("channel_label") or ""
        if codec_lbl in ("TrueHD", "DTS-HD MA", "EAC3", "FLAC", "DTS"):
            badges.append({
                "id": "audio_hq",
                "label": f"{codec_lbl} {ch_lbl}".strip(),
                "category": "audio",
                "tone": "indigo",
            })

    return badges


def build_movie_entry(
    disc: Dict[str, Any],
    fetch_online_metadata: bool = False,
) -> Dict[str, Any]:
    """Assemble full movie dictionary from discovery item."""
    video_path = disc["video_path"]
    rel_video = disc["rel_video"]
    folder = disc["folder"]
    display_folder = disc["display_folder"]
    video_filename = disc["video_filename"]

    # 1. Release parser (combine folder + filename context)
    rel_name_input = video_filename if len(video_filename) >= len(display_folder) else f"{display_folder} {video_filename}"
    rel_info = release_parser.parse_release_name(rel_name_input)
    if not rel_info.get("year"):
        folder_parsed = release_parser.parse_release_name(display_folder)
        if folder_parsed.get("year"):
            rel_info["year"] = folder_parsed["year"]
            if folder_parsed.get("clean_title"):
                rel_info["clean_title"] = folder_parsed["clean_title"]

    # 2. Media probe (cached by path + size + mtime)
    probe = media_probe.probe_file(video_path)

    # 3. Subtitles scan (embedded + sidecars + provenance + sync confidence)
    sub_summary = subtitles.summarize_movie_subtitles(
        video_path=video_path,
        media_info=probe,
        release_info=rel_info,
    )

    # 4. Cached online subtitle status
    online_sub = get_online_sub_status(rel_video)

    # 5. Movie metadata (local NFO / disk cache / optional TMDb or keyless lookup)
    meta = metadata.get_movie_metadata(
        video_path=video_path,
        folder_name=display_folder,
        release_info=rel_info,
        fetch_online=fetch_online_metadata,
    )

    # Check for local poster image (.jpg/.jpeg/.png/.webp) inside the movie folder
    local_poster_url = None
    folder_path = disc.get("folder_path")
    if folder and folder_path and os.path.isdir(folder_path):
        try:
            img_files = [
                f for f in sorted(os.listdir(folder_path))
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                and not f.startswith(".")
            ]
            if img_files:
                # Prefer poster.jpg / cover.jpg or first .jpg
                preferred = next(
                    (f for f in img_files if os.path.splitext(f)[0].lower() in ("poster", "cover", "folder", display_folder.lower())),
                    img_files[0],
                )
                local_poster_url = f"/get_poster/{folder}/{preferred}"
        except OSError:
            pass

    poster_url = local_poster_url or meta.get("poster_url")

    # Background auto-enrichment (like anime.pi): if a movie was copied directly to the SSD
    # without a local .jpg or cached metadata, fetch metadata & save <folder>/<folder>.jpg in background.
    if not fetch_online_metadata and (not poster_url or not meta.get("overview")):
        _bg_enrich_key = f"enrich:{video_path}"
        if not getattr(build_movie_entry, "_inflight", None):
            build_movie_entry._inflight = set()  # type: ignore[attr-defined]
        if _bg_enrich_key not in build_movie_entry._inflight:  # type: ignore[attr-defined]
            build_movie_entry._inflight.add(_bg_enrich_key)  # type: ignore[attr-defined]

            def _enrich_bg(vpath: str, d_folder: str, f_path: Optional[str], r_info: Dict[str, Any], has_local_img: bool) -> None:
                try:
                    enriched = metadata.get_movie_metadata(
                        video_path=vpath,
                        folder_name=d_folder,
                        release_info=r_info,
                        fetch_online=True,
                    )
                    if not has_local_img and f_path and os.path.isdir(f_path):
                        raw_info = metadata.lookup(
                            r_info.get("clean_title") or d_folder,
                            year=r_info.get("year"),
                            folder_path=f_path,
                        )
                        remote_poster = (raw_info or {}).get("poster") or ""
                        if remote_poster.startswith("http"):
                            dest_jpg = os.path.join(f_path, f"{d_folder}.jpg")
                            if not os.path.exists(dest_jpg):
                                metadata.fetch_poster(remote_poster, dest_jpg)
                except Exception:
                    pass

            threading.Thread(
                target=_enrich_bg,
                args=(video_path, display_folder, folder_path if folder else None, rel_info, bool(local_poster_url)),
                daemon=True,
            ).start()

    # 6. Resume position & preferences
    abs_norm = os.path.abspath(video_path).replace("\\", "/")
    resume = get_resume_entry(
        rel_video,
        fallback_keys=[abs_norm, os.path.basename(video_path), f"{folder}/{video_filename}".strip("/")],
    )
    position = float(resume.get("position", 0.0) or 0.0)
    duration = float(probe.get("duration") or resume.get("duration") or 0.0)
    progress_pct = round(min(100.0, (position / duration) * 100.0), 1) if duration > 0 and position > 0 else 0.0

    # 7. Badges & capability flags
    badges = _build_badges(probe, rel_info, sub_summary, online_sub)
    is_4k = bool(probe.get("is_4k") or rel_info.get("is_4k"))
    hdr_type = probe.get("hdr_type") if probe.get("hdr_type") not in (None, "SDR") else rel_info.get("hdr")
    is_hdr = bool(hdr_type and hdr_type != "SDR")
    is_remux = bool(probe.get("is_remux") or rel_info.get("is_remux"))
    has_hr_local = bool(sub_summary.get("has_hr_local"))
    has_hr_embedded = any(t.get("source") == "embedded" for t in sub_summary.get("hr_tracks", []))
    has_hr_sidecar = any(t.get("source") == "sidecar" for t in sub_summary.get("hr_tracks", []))
    has_hr_downloadable = bool(online_sub.get("hr_available_online"))
    has_en_local = bool(sub_summary.get("has_en_local"))
    multi_audio = bool(probe.get("multi_audio"))

    # Determine browser direct-play vs compatible remux need
    audio_tracks = probe.get("audio_tracks") or []
    browser_compatible_audio_codecs = {"aac", "mp3", "opus", "vorbis", "flac"}
    needs_browser_audio_remux = bool(
        audio_tracks
        and not any((a.get("codec") or "").lower() in browser_compatible_audio_codecs for a in audio_tracks)
    )

    movie_id = _make_movie_id(rel_video)
    try:
        mtime = int(os.path.getmtime(video_path))
    except OSError:
        mtime = 0

    return {
        "id": movie_id,
        "folder": folder,  # Legacy compatibility ("" if loose file in MOVIES_DIR)
        "display_folder": display_folder,
        "file": video_filename,  # Relative to folder (or MOVIES_DIR if folder == "")
        "files": [video_filename] + [e["filename"] for e in disc.get("extra_videos", [])],
        "rel_video": rel_video,
        "video": rel_video,  # Legacy compatibility with /api/movies
        "video_path": video_path,
        "mtime": mtime,
        # Display metadata
        "title": meta.get("title") or rel_info.get("clean_title") or display_folder,
        "original_title": meta.get("original_title") or "",
        "year": meta.get("year") or rel_info.get("year"),
        "edition": rel_info.get("edition"),
        "overview": meta.get("overview") or "",
        "tagline": meta.get("tagline") or "",
        "genres": meta.get("genres") or [],
        "rating": meta.get("rating"),
        "runtime_minutes": meta.get("runtime_minutes") or (int(round(duration / 60.0)) if duration > 0 else None),
        "directors": meta.get("directors") or [],
        "cast": meta.get("cast") or [],
        "poster_url": poster_url,
        "poster": poster_url or "",  # Legacy compatibility with /api/movies
        "watched": position > 0,
        "resume_time": position,
        "backdrop_url": meta.get("backdrop_url"),
        "imdb_id": meta.get("imdb_id") or "",
        "tmdb_id": meta.get("tmdb_id"),
        "metadata_source": meta.get("metadata_source") or "filename",
        # Technical / playback capabilities
        "is_4k": is_4k,
        "is_hdr": is_hdr,
        "hdr_type": hdr_type or "SDR",
        "is_remux": is_remux,
        "resolution": probe.get("resolution_label") or rel_info.get("resolution") or "Unknown",
        "video_summary": probe.get("video_summary") or "",
        "audio_summary": probe.get("audio_summary") or "",
        "multi_audio": multi_audio,
        "audio_track_count": len(audio_tracks),
        "needs_browser_audio_remux": needs_browser_audio_remux,
        "size_bytes": probe.get("size_bytes") or 0,
        "size_human": probe.get("size_human") or "",
        "duration": duration,
        "bitrate_mbps": probe.get("bitrate_mbps") or 0.0,
        "moviehash": probe.get("moviehash") or "",
        "video_tracks": probe.get("video_tracks") or [],
        "audio_tracks": audio_tracks,
        "chapters": probe.get("chapters") or [],
        "playback_warnings": probe.get("playback_warnings") or rel_info.get("warnings") or [],
        "release": rel_info,
        # Subtitles & Croatian first-class indexing
        "has_hr_subs": has_hr_local,
        "has_hr_subs_local": has_hr_local,
        "has_hr_subs_embedded": has_hr_embedded,
        "has_hr_subs_sidecar": has_hr_sidecar,
        "has_hr_subs_downloadable": has_hr_downloadable,
        "hr_sync_confidence": sub_summary.get("best_hr_sync"),
        "hr_subtitle_status": (
            "local_exact"
            if has_hr_local and sub_summary.get("best_hr_sync") in ("exact", "embedded")
            else "local_available"
            if has_hr_local
            else "downloadable_verified"
            if has_hr_downloadable
            else "searchable_online"
        ),
        "has_en_subs": has_en_local,
        "has_en_subs_local": has_en_local,
        "subtitle_count": len(sub_summary.get("tracks") or []),
        "subtitle_languages": sub_summary.get("languages") or [],
        "subtitles": sub_summary.get("tracks") or [],
        "online_subtitles": online_sub,
        # UI Badges
        "badges": badges,
        # Playback resume & preferences
        "resume_position": position,
        "resume_percent": progress_pct,
        "sub_delay": float(resume.get("sub_delay", 0.0) or 0.0),
        "preferred_sid": resume.get("sid"),
        "preferred_aid": resume.get("aid"),
        "preferred_audio_mode": resume.get("audio_mode"),
        "last_played_at": resume.get("updated_at"),
    }


# ─── Library Query, Filter & Search ───────────────────────────────────────────

def scan_library(
    search_query: str = "",
    filter_by: str = "all",
    sort_by: str = "title",
    fetch_online_metadata: bool = False,
) -> Dict[str, Any]:
    """Scan movie library, compute filter counts, and return filtered/sorted movies."""
    discovered = _discover_video_items()
    all_movies = [build_movie_entry(d, fetch_online_metadata=fetch_online_metadata) for d in discovered]

    # Compute aggregate library counts before filtering
    counts = {
        "all": len(all_movies),
        "hr_local": sum(1 for m in all_movies if m["has_hr_subs_local"]),
        "hr_downloadable": sum(1 for m in all_movies if not m["has_hr_subs_local"] and m["has_hr_subs_downloadable"]),
        "hr_any": sum(1 for m in all_movies if m["has_hr_subs_local"] or m["has_hr_subs_downloadable"]),
        "en_local": sum(1 for m in all_movies if m["has_en_subs_local"]),
        "4k": sum(1 for m in all_movies if m["is_4k"]),
        "hdr": sum(1 for m in all_movies if m["is_hdr"]),
        "remux": sum(1 for m in all_movies if m["is_remux"]),
        "multi_audio": sum(1 for m in all_movies if m["multi_audio"]),
        "watching": sum(1 for m in all_movies if 1.0 <= m["resume_percent"] < 92.0),
        "unwatched": sum(1 for m in all_movies if m["resume_percent"] < 1.0),
    }

    # Apply filter
    filtered = all_movies
    f_key = (filter_by or "all").strip().lower()
    if f_key in ("hr", "hr_local", "croatian", "croatian_local"):
        filtered = [m for m in filtered if m["has_hr_subs_local"]]
    elif f_key in ("hr_downloadable", "croatian_downloadable", "hr_online"):
        filtered = [m for m in filtered if not m["has_hr_subs_local"] and m["has_hr_subs_downloadable"]]
    elif f_key in ("hr_any", "croatian_any", "hr_all"):
        filtered = [m for m in filtered if m["has_hr_subs_local"] or m["has_hr_subs_downloadable"]]
    elif f_key in ("en", "en_local", "english"):
        filtered = [m for m in filtered if m["has_en_subs_local"]]
    elif f_key in ("4k", "uhd", "2160p"):
        filtered = [m for m in filtered if m["is_4k"]]
    elif f_key in ("hdr", "hdr10", "dv", "dolby_vision"):
        filtered = [m for m in filtered if m["is_hdr"]]
    elif f_key in ("remux", "bluray_remux"):
        filtered = [m for m in filtered if m["is_remux"]]
    elif f_key in ("multi_audio", "dual_audio"):
        filtered = [m for m in filtered if m["multi_audio"]]
    elif f_key in ("watching", "continue", "in_progress"):
        filtered = [m for m in filtered if 1.0 <= m["resume_percent"] < 92.0]
    elif f_key == "unwatched":
        filtered = [m for m in filtered if m["resume_percent"] < 1.0]

    # Apply text search
    q = (search_query or "").strip().lower()
    if q:
        def _matches(m: Dict[str, Any]) -> bool:
            haystack = " ".join([
                str(m.get("title") or ""),
                str(m.get("original_title") or ""),
                str(m.get("display_folder") or ""),
                str(m.get("file") or ""),
                str(m.get("year") or ""),
                " ".join(m.get("genres") or []),
                " ".join(m.get("directors") or []),
                " ".join(m.get("cast") or []),
                str(m.get("hdr_type") or ""),
                str(m.get("resolution") or ""),
                "croatian hr hrvatski" if m.get("has_hr_subs_local") else "",
                "remux" if m.get("is_remux") else "",
            ]).lower()
            return all(tok in haystack for tok in q.split())

        filtered = [m for m in filtered if _matches(m)]

    # Apply sorting
    s_key = (sort_by or "title").strip().lower()
    if s_key == "year":
        filtered.sort(key=lambda m: (-(m.get("year") or 0), (m.get("title") or "").lower()))
    elif s_key in ("recent", "added", "mtime"):
        filtered.sort(key=lambda m: (-int(m.get("mtime") or 0), (m.get("title") or "").lower()))
    elif s_key == "size":
        filtered.sort(key=lambda m: (-int(m.get("size_bytes") or 0), (m.get("title") or "").lower()))
    elif s_key == "rating":
        filtered.sort(key=lambda m: (-(float(m.get("rating") or 0.0)), (m.get("title") or "").lower()))
    elif s_key == "last_played":
        filtered.sort(key=lambda m: (-int(m.get("last_played_at") or 0), (m.get("title") or "").lower()))
    else:
        filtered.sort(key=lambda m: (m.get("title") or "").lower())

    return {
        "movies": filtered,
        "counts": counts,
        "filter": f_key,
        "sort": s_key,
        "query": search_query,
        "storage_warning": config.storage_problem(),
    }


def find_movie(
    identifier: str = "",
    folder: Optional[str] = None,
    file: Optional[str] = None,
    rel_video: Optional[str] = None,
    fetch_online_metadata: bool = False,
) -> Optional[Dict[str, Any]]:
    """Locate a single movie by ID, rel_video, or legacy (folder, file) pair."""
    discovered = _discover_video_items()
    ident = (identifier or "").strip()
    rel_req = (rel_video or "").strip().strip("/")
    folder_req = (folder or "").strip().strip("/")
    file_req = (file or "").strip().strip("/")

    for d in discovered:
        d_id = _make_movie_id(d["rel_video"])
        d_rel = d["rel_video"].strip("/")
        d_folder = d["folder"].strip("/")
        d_disp = d["display_folder"].strip("/")
        d_file = d["video_filename"].strip("/")

        if ident and (ident == d_id or ident == d_rel or ident == d_folder or ident == d_disp or ident == d_file):
            return build_movie_entry(d, fetch_online_metadata=fetch_online_metadata)
        if rel_req and (rel_req == d_rel or rel_req == f"{d_folder}/{d_file}".strip("/")):
            return build_movie_entry(d, fetch_online_metadata=fetch_online_metadata)
        if folder_req or file_req:
            if folder_req and file_req:
                if (d_folder == folder_req or d_disp == folder_req) and (d_file == file_req or os.path.basename(d_file) == file_req):
                    return build_movie_entry(d, fetch_online_metadata=fetch_online_metadata)
            elif folder_req and (d_folder == folder_req or d_disp == folder_req):
                return build_movie_entry(d, fetch_online_metadata=fetch_online_metadata)
            elif file_req and (d_file == file_req or os.path.basename(d_file) == file_req or d_rel == file_req):
                return build_movie_entry(d, fetch_online_metadata=fetch_online_metadata)

    return None


def resolve_safe_media_path(rel_path: str) -> Optional[str]:
    """Resolve a relative path within MOVIES_DIR or EXTRA_MOVIES_DIRS safely without traversal."""
    clean_rel = (rel_path or "").strip().lstrip("/")
    if not clean_rel or ".." in clean_rel.split("/"):
        return None
    roots = [config.MOVIES_DIR] + config.EXTRA_MOVIES_DIRS
    for root in roots:
        if not os.path.isdir(root):
            continue
        root_real = os.path.realpath(root)
        candidate = os.path.realpath(os.path.join(root_real, clean_rel))
        if candidate.startswith(root_real + os.sep) and os.path.isfile(candidate):
            return candidate
    return None


def rename_movie_folder(old_name: str, new_name: str) -> Tuple[bool, str]:
    """Safely rename a movie folder or loose movie file in MOVIES_DIR."""
    old_clean = (old_name or "").strip().strip("/")
    new_clean = os.path.basename((new_name or "").strip())
    if not old_clean or not new_clean or new_clean in (".", "..") or "/" in new_clean or "\\" in new_clean:
        return False, "Invalid folder or file name"
    if old_clean in config.IGNORED_DIRS or new_clean in config.IGNORED_DIRS:
        return False, "Reserved directory name"

    root_real = os.path.realpath(config.MOVIES_DIR)
    old_path = os.path.realpath(os.path.join(root_real, old_clean))
    if not old_path.startswith(root_real + os.sep) or not os.path.exists(old_path):
        return False, "Source movie not found"

    if os.path.isfile(old_path):
        ext = os.path.splitext(old_path)[1]
        if not new_clean.lower().endswith(ext.lower()):
            new_clean = new_clean + ext
    new_path = os.path.realpath(os.path.join(os.path.dirname(old_path), new_clean))
    if not new_path.startswith(root_real + os.sep):
        return False, "Invalid destination path"
    if os.path.exists(new_path):
        return False, "A movie with that name already exists"

    try:
        os.rename(old_path, new_path)
        return True, new_clean
    except OSError as e:
        return False, str(e)


def delete_movie_entry(identifier_or_folder: str) -> Tuple[bool, str]:
    """Safely delete a movie folder or loose file from MOVIES_DIR."""
    movie = find_movie(identifier=identifier_or_folder, folder=identifier_or_folder)
    root_real = os.path.realpath(config.MOVIES_DIR)
    if movie:
        if movie.get("folder"):
            target = os.path.realpath(os.path.join(root_real, movie["folder"]))
        else:
            target = os.path.realpath(movie["video_path"])
    else:
        clean = (identifier_or_folder or "").strip().strip("/")
        if not clean or clean in config.IGNORED_DIRS:
            return False, "Invalid movie identifier"
        target = os.path.realpath(os.path.join(root_real, clean))

    if not target.startswith(root_real + os.sep) or not os.path.exists(target):
        return False, "Movie not found"

    try:
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.unlink(target)
        return True, "Deleted"
    except OSError as e:
        return False, str(e)
