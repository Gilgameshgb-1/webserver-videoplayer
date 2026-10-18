"""Huberry Stream (stream.pi) Flask API & Streaming Server (`remote.py`).

Provides:
- Movie library scanning, filtering (Croatian subtitles local/downloadable,
  4K UHD, HDR10/DV, Blu-ray Remux, multiple audio tracks), and metadata enrichment
- Hardware-accelerated TV playback control via `player.py` (`wserVideoPlayer` / `mpv` IPC)
  with runtime track enumeration, sidecar attachment, live subtitle delay, and
  7.1 TrueHD/DTS-HD MA audio downmix / HDMI passthrough modes
- Browser direct range streaming (`/stream/<path>`) with 512 KB chunks AND
  zero-video-transcode compatible audio remuxing (`/stream-compat/<path>`)
  for Blu-ray remuxes with TrueHD/DTS/AC3 audio
- Multilingual & Croatian subtitle discovery, encoding normalization (`cp1250`/
  `iso-8859-2` -> `utf-8`), embedded MKV text subtitle extraction to WebVTT,
  provenance & release sync confidence tracking, and external download/upload
- Decoupled media acquisition endpoints (`/api/acquisition/*` & `/api/torrent/*`)
- Full backwards compatibility with all original Huberry Stream routes
"""

import mimetypes
import os
import re
import shutil
import subprocess
from typing import Any, Dict, Generator, Optional

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)

import acquisition
import config
import library
import media_probe
import metadata
import player
import release_parser
import subtitles

app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/static",
)

# Currently playing movie context
_current_movie_rel: Optional[str] = None


def _resolve_movie_from_request(data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    payload = data if data is not None else (request.get_json(silent=True) or {})
    movie_id = payload.get("id") or request.args.get("id") or ""
    rel_video = (
        payload.get("rel_video")
        or payload.get("rel")
        or payload.get("video")
        or request.args.get("rel")
        or request.args.get("video")
        or ""
    )
    folder = payload.get("folder") if "folder" in payload else request.args.get("folder")
    file = payload.get("file") if "file" in payload else request.args.get("file")
    return library.find_movie(
        identifier=str(movie_id),
        folder=folder,
        file=file,
        rel_video=str(rel_video),
    )


# ─── Frontend Entry & Static / Poster Assets ──────────────────────────────────

@app.route("/")
def index() -> str:
    scan_res = library.scan_library()
    return render_template("index.html", movies=scan_res["movies"], counts=scan_res["counts"])


@app.route("/assets/<path:filename>")
def serve_assets(filename: str):
    return send_from_directory(config.ASSETS_DIR, filename)


@app.route("/get_poster/<folder>/<path:filename>")
def get_poster(folder: str, filename: str):
    rel = f"{folder}/{filename}".strip("/")
    full_path = library.resolve_safe_media_path(rel)
    if not full_path:
        abort(404)
    return send_file(full_path)


@app.route("/poster/<path:filename>")
def serve_poster(filename: str):
    safe_name = os.path.basename(filename)
    full_path = os.path.join(config.POSTER_CACHE_DIR, safe_name)
    if not os.path.isfile(full_path):
        abort(404)
    resp = send_file(full_path, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


# ─── Health & Diagnostics ─────────────────────────────────────────────────────

@app.route("/api/health")
def api_health():
    disk_info = {}
    try:
        target_dir = config.MOVIES_DIR if os.path.isdir(config.MOVIES_DIR) else "/"
        usage = shutil.disk_usage(target_dir)
        disk_info = {
            "total_gb": round(usage.total / (1024 ** 3), 1),
            "used_gb": round(usage.used / (1024 ** 3), 1),
            "free_gb": round(usage.free / (1024 ** 3), 1),
            "percent_used": round((usage.used / usage.total) * 100.0, 1) if usage.total else 0.0,
        }
    except OSError:
        pass

    return jsonify({
        "status": "ok",
        "movies_dir": config.MOVIES_DIR,
        "mpv_ipc": config.MPV_IPC_PATH,
        "player_exe": config.MPV_EXE,
        "player_running": player.is_running(),
        "storage_warning": config.storage_problem(),
        "disk": disk_info,
        "headless": config.HEADLESS,
        "tools": {
            "ffprobe": bool(shutil.which("ffprobe")),
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "mpv": bool(shutil.which("mpv")),
            "wserVideoPlayer": os.path.isfile(config.PLAYER_BINARY),
            "libtorrent": acquisition.HAS_LIBTORRENT,
        },
        "integrations": {
            "tmdb_configured": bool(config.TMDB_API_KEY),
            "opensubtitles_configured": bool(config.OPENSUBTITLES_API_KEY),
            "subdl_configured": bool(config.SUBDL_API_KEY),
            "podnapisi_keyless": True,
        },
    })


# ─── Library & Movie Detail API ───────────────────────────────────────────────

@app.route("/api/library")
def get_library():
    q = request.args.get("q", "").strip()
    filter_by = request.args.get("filter", "all").strip()
    sort_by = request.args.get("sort", "title").strip()
    online_meta = request.args.get("online_meta", "0") == "1"

    result = library.scan_library(
        search_query=q,
        filter_by=filter_by,
        sort_by=sort_by,
        fetch_online_metadata=online_meta,
    )
    if request.args.get("format") == "legacy":
        return jsonify(result["movies"])
    return jsonify(result)


@app.route("/api/movies")
def api_movies():
    """Legacy Huberry Stream endpoint returning the list of scanned movies."""
    result = library.scan_library()
    return jsonify(result["movies"])


@app.route("/api/movieinfo")
def movie_info():
    """Legacy Huberry Stream flip-card info endpoint backed by `metadata.py`."""
    title = request.args.get("title", "").strip()
    if not title:
        return jsonify({"found": False})
    info = metadata.lookup(title)
    if not info:
        return jsonify({"found": False})
    return jsonify({
        "found": True,
        "rating": info.get("rating") or "N/A",
        "synopsis": info.get("synopsis") or "",
        "genre": info.get("genres") or [],
        "year": info.get("year") or "",
        "director": info.get("director") or "",
        "poster": info.get("poster") or "",
    })


@app.route("/api/movie")
@app.route("/api/movies/<path:movie_id>")
def get_movie_detail(movie_id: str = ""):
    enrich = request.args.get("enrich", "1") == "1"
    check_subs = request.args.get("check_online_subs", "0") == "1"
    ident = movie_id or request.args.get("id", "")
    movie = library.find_movie(
        identifier=ident,
        folder=request.args.get("folder") or (movie_id if movie_id else None),
        file=request.args.get("file"),
        rel_video=request.args.get("rel", "") or request.args.get("video", "") or movie_id,
        fetch_online_metadata=enrich,
    )
    if not movie:
        return jsonify({"error": "Movie not found"}), 404

    if check_subs:
        sub_check = library.check_online_subtitles_for_movie(
            video_path=movie["video_path"],
            rel_video=movie["rel_video"],
            title=movie.get("title") or "",
            year=movie.get("year"),
            imdb_id=movie.get("imdb_id") or "",
            tmdb_id=movie.get("tmdb_id"),
        )
        movie = library.find_movie(rel_video=movie["rel_video"], fetch_online_metadata=False) or movie
        movie["online_subtitles_search"] = sub_check["search_response"]

    return jsonify(movie)


@app.route("/api/movie/metadata/refresh", methods=["POST"])
def refresh_movie_metadata():
    data = request.get_json(silent=True) or {}
    movie = _resolve_movie_from_request(data)
    if not movie:
        return jsonify({"error": "Movie not found"}), 404

    metadata.get_movie_metadata(
        video_path=movie["video_path"],
        folder_name=movie["display_folder"],
        release_info=movie.get("release"),
        fetch_online=True,
        force_refresh=True,
    )
    updated = library.find_movie(rel_video=movie["rel_video"], fetch_online_metadata=False)
    return jsonify({"status": "refreshed", "movie": updated})


@app.route("/api/rename", methods=["POST"])
def rename_movie():
    data = request.get_json(silent=True) or {}
    old_name = data.get("old_name") or data.get("folder") or ""
    new_name = data.get("new_name") or ""
    ok, msg = library.rename_movie_folder(old_name, new_name)
    if not ok:
        return jsonify({"error": msg}), 400
    return jsonify({"status": "renamed", "new_name": msg})


@app.route("/api/delete", methods=["POST"])
def delete_movie():
    global _current_movie_rel
    data = request.get_json(silent=True) or {}
    ident = data.get("id") or data.get("folder") or data.get("rel_video") or ""
    movie = _resolve_movie_from_request(data)
    if movie and _current_movie_rel == movie.get("rel_video"):
        player.stop()
        _current_movie_rel = None

    ok, msg = library.delete_movie_entry(ident)
    if not ok:
        return jsonify({"error": msg}), 400
    return jsonify({"status": "deleted"})


# ─── TV Playback & Hardware Player Control (wserVideoPlayer / mpv) ───────────

@app.route("/api/play", methods=["POST"])
def play_movie():
    global _current_movie_rel
    data = request.get_json(silent=True) or {}
    movie = _resolve_movie_from_request(data)
    if not movie:
        return jsonify({"error": "Movie not found"}), 404

    video_path = movie["video_path"]
    rel_video = movie["rel_video"]

    start_pos = 0.0
    if "start_pos" in data and data["start_pos"] is not None:
        start_pos = float(data["start_pos"])
    elif data.get("resume", True):
        saved_pos = float(movie.get("resume_position") or 0.0)
        duration = float(movie.get("duration") or 0.0)
        if saved_pos > 5.0 and (duration <= 0 or saved_pos < duration * 0.95):
            start_pos = saved_pos

    sidecar_paths = [
        t["path"]
        for t in (movie.get("subtitles") or [])
        if t.get("source") == "sidecar" and t.get("path") and os.path.isfile(t["path"])
    ]

    sub_delay = float(data.get("sub_delay", movie.get("sub_delay", 0.0)) or 0.0)
    sid = data.get("sid", movie.get("preferred_sid"))
    aid = data.get("aid", movie.get("preferred_aid"))
    audio_mode = data.get("audio_mode") or movie.get("preferred_audio_mode") or "auto-safe"

    play_res = player.play_file(
        video_path=video_path,
        start_pos=start_pos,
        sidecar_subs=sidecar_paths,
        sid=sid,
        aid=aid,
        sub_delay=sub_delay,
        audio_mode=audio_mode,
    )

    prefer_lang = data.get("prefer_lang")
    if prefer_lang and sid is None:
        tracks = (play_res.get("status") or {}).get("tracks") or {}
        sub_list = tracks.get("sub") or []
        for st in sub_list:
            if (st.get("lang") or "").lower() in (prefer_lang.lower(), "hrv", "scr"):
                player.set_subtitle_track(st["id"])
                break

    _current_movie_rel = rel_video
    library.update_resume_entry(
        rel_video,
        position=start_pos,
        duration=movie.get("duration") or None,
        sub_delay=sub_delay,
        audio_mode=audio_mode,
    )
    return jsonify({
        "status": "playing",
        "file": movie["file"],
        "rel_video": rel_video,
        "title": movie["title"],
        "resumed_at": start_pos,
        "player": play_res.get("status"),
    })


@app.route("/api/load")
def api_load():
    """Legacy Huberry Stream GET /api/load?file=... endpoint, upgraded with sidecar sub-add."""
    global _current_movie_rel
    rel = request.args.get("file", "").strip()
    movie = library.find_movie(rel_video=rel, file=rel)
    if not movie:
        full = library.resolve_safe_media_path(rel)
        if not full:
            return "forbidden", 403
        player.play_file(full)
        _current_movie_rel = rel
        return "ok"

    sidecar_paths = [
        t["path"]
        for t in (movie.get("subtitles") or [])
        if t.get("source") == "sidecar" and t.get("path") and os.path.isfile(t["path"])
    ]
    start_pos = float(movie.get("resume_position") or 0.0)
    sub_delay = float(movie.get("sub_delay") or 0.0)
    player.play_file(
        video_path=movie["video_path"],
        start_pos=start_pos,
        sidecar_subs=sidecar_paths,
        sub_delay=sub_delay,
    )
    _current_movie_rel = movie["rel_video"]
    return "ok"


@app.route("/api/status")
def get_status():
    global _current_movie_rel
    st = player.get_status()
    pos = float(st.get("position") or 0.0)
    dur = float(st.get("duration") or 0.0)
    pct = round((pos / dur) * 100.0, 2) if dur > 0 else 0.0

    total_seconds = max(0, int(pos))
    m_val, s_val = divmod(total_seconds, 60)
    h_val, m_val = divmod(m_val, 60)
    st["time"] = f"{h_val}:{m_val:02d}:{s_val:02d}"
    st["percent"] = pct

    if st.get("running") and not st.get("idle") and pos > 1.0:
        key = _current_movie_rel or st.get("filename") or ""
        if key:
            library.update_resume_entry(
                key,
                position=pos,
                duration=dur if dur > 0 else None,
                sub_delay=st.get("sub_delay"),
            )

    st["current_movie_rel"] = _current_movie_rel
    return jsonify(st)


@app.route("/files/<action>")
def file_action(action: str):
    if action == "play-pause":
        player.toggle_pause()
        return "ok"
    if action == "backward":
        player.seek_relative(-10)
        return "ok"
    if action == "forward":
        player.seek_relative(10)
        return "ok"
    return "error", 400


@app.route("/api/stop")
def stop_movie():
    global _current_movie_rel
    st = player.get_status()
    pos = float(st.get("position") or 0.0)
    dur = float(st.get("duration") or 0.0)
    key = _current_movie_rel or st.get("filename") or ""
    if key and pos > 1.0:
        library.update_resume_entry(key, position=pos, duration=dur if dur > 0 else None)
    player.stop()
    _current_movie_rel = None
    return "ok"


@app.route("/api/quit")
def quit_mpv():
    global _current_movie_rel
    st = player.get_status()
    pos = float(st.get("position") or 0.0)
    dur = float(st.get("duration") or 0.0)
    key = _current_movie_rel or st.get("filename") or ""
    if key and pos > 1.0:
        library.update_resume_entry(key, position=pos, duration=dur if dur > 0 else None)
    player.quit_player()
    _current_movie_rel = None
    return "ok"


@app.route("/api/seek", methods=["POST"])
def seek_movie():
    data = request.get_json(silent=True) or {}
    pos = float(data.get("position", 0.0) or 0.0)
    ok = player.seek_absolute(pos)
    if _current_movie_rel and pos >= 0:
        library.update_resume_entry(_current_movie_rel, position=pos)
    return jsonify({"status": "seeked" if ok else "failed", "position": pos})


@app.route("/api/tracks")
def get_runtime_tracks():
    st = player.get_status()
    return jsonify({
        "tracks": st.get("tracks", {"video": [], "audio": [], "sub": []}),
        "aid": st.get("aid"),
        "sid": st.get("sid"),
        "sub_delay": st.get("sub_delay", 0.0),
    })


@app.route("/api/track", methods=["POST"])
@app.route("/api/player/subtitle", methods=["POST"])
@app.route("/api/player/audio", methods=["POST"])
def select_runtime_track():
    data = request.get_json(silent=True) or {}
    if request.path.endswith("/audio") and "mode" in data and "aid" not in data and "id" not in data:
        mode = (data.get("mode") or "auto-safe").strip().lower()
        res = player.set_audio_output_mode(mode)
        if _current_movie_rel:
            library.update_resume_entry(_current_movie_rel, audio_mode=res.get("mode"))
        return jsonify(res)

    if "sid" in data:
        track_type = "sub"
        track_id = data.get("sid")
    elif "aid" in data:
        track_type = "audio"
        track_id = data.get("aid")
    else:
        track_type = (data.get("type") or ("audio" if request.path.endswith("/audio") else "sub")).lower()
        track_id = data.get("id")
    sub_path = data.get("path")

    if track_type in ("sub", "sid", "subtitle"):
        if sub_path and os.path.isfile(sub_path):
            ok = player.add_subtitle_file(sub_path, select=True)
        else:
            ok = player.set_subtitle_track(track_id)
        if _current_movie_rel:
            library.update_resume_entry(_current_movie_rel, sid=track_id)
        return jsonify({"status": "ok" if ok else "failed", "type": "sub", "id": track_id})

    if track_type in ("audio", "aid"):
        ok = player.set_audio_track(track_id)
        if _current_movie_rel:
            library.update_resume_entry(_current_movie_rel, aid=track_id)
        return jsonify({"status": "ok" if ok else "failed", "type": "audio", "id": track_id})

    return jsonify({"error": "Invalid track type"}), 400


@app.route("/api/sub-delay", methods=["POST"])
@app.route("/api/player/sub-delay", methods=["POST"])
def update_sub_delay():
    data = request.get_json(silent=True) or {}
    movie = _resolve_movie_from_request(data)
    rel_key = (movie and movie.get("rel_video")) or _current_movie_rel or data.get("rel_video") or data.get("file") or ""

    if "delta" in data and data["delta"] is not None:
        delta = float(data["delta"])
        new_delay = player.adjust_sub_delay(delta)
        if not player.is_running() and rel_key:
            cur = float(library.get_resume_entry(rel_key).get("sub_delay", 0.0) or 0.0)
            new_delay = round(cur + delta, 3)
    else:
        new_delay = round(float(data.get("delay", 0.0) or 0.0), 3)
        player.set_sub_delay(new_delay)

    if rel_key:
        library.update_resume_entry(rel_key, sub_delay=new_delay)

    return jsonify({"status": "ok", "sub_delay": new_delay, "rel_video": rel_key})


@app.route("/api/audio-mode", methods=["POST"])
def update_audio_mode():
    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "auto-safe").strip().lower()
    res = player.set_audio_output_mode(mode)
    if _current_movie_rel:
        library.update_resume_entry(_current_movie_rel, audio_mode=res.get("mode"))
    return jsonify(res)


@app.route("/api/command", methods=["GET", "POST"])
def control_player():
    global _current_movie_rel
    if request.method == "GET":
        query = request.args.get("q", "").strip()
        if query:
            player.command(["osd-msg-bar"] + query.split(" "))
        return "ok"

    data = request.get_json(silent=True) or {}
    action = data.get("action")

    if action == "pause":
        player.toggle_pause()
    elif action == "stop":
        st = player.get_status()
        pos = float(st.get("position") or 0.0)
        dur = float(st.get("duration") or 0.0)
        key = _current_movie_rel or st.get("filename") or ""
        if key and pos > 1.0:
            library.update_resume_entry(key, position=pos, duration=dur if dur > 0 else None)
        player.stop()
        _current_movie_rel = None
    elif action == "seek":
        val = float(data.get("value", 0) or 0)
        player.seek_relative(val)
    elif action == "set_track":
        ttype = data.get("type")
        tid = data.get("id")
        if ttype in ("sid", "sub"):
            player.set_subtitle_track(tid)
        elif ttype in ("aid", "audio"):
            player.set_audio_track(tid)
    elif action == "volume":
        player.set_volume(float(data.get("value", 100) or 100))
    elif action == "sub_delay":
        if "delta" in data:
            player.adjust_sub_delay(float(data["delta"]))
        else:
            player.set_sub_delay(float(data.get("value", 0.0) or 0.0))
    elif action == "audio_mode":
        player.set_audio_output_mode(str(data.get("mode", "auto-safe")))
    elif action == "chapter":
        player.set_chapter(int(data.get("value", 0) or 0))

    return jsonify({"status": "command_sent", "action": action})


# ─── Browser Streaming (Direct Range + Compatible Remux Audio) ────────────────

@app.route("/api/progress", methods=["POST"])
def save_browser_progress():
    data = request.get_json(silent=True) or {}
    key = data.get("rel_video") or data.get("file") or ""
    if not key:
        return jsonify({"error": "Missing rel_video or file"}), 400
    pos = data.get("position")
    dur = data.get("duration")
    delay = data.get("sub_delay")
    entry = library.update_resume_entry(
        key,
        position=float(pos) if pos is not None else None,
        duration=float(dur) if dur is not None else None,
        sub_delay=float(delay) if delay is not None else None,
    )
    return jsonify({"status": "saved", "entry": entry})


@app.route("/api/resume_get")
def resume_get():
    filename = request.args.get("file", "").strip()
    if not filename:
        return jsonify({"time": 0, "sub_delay": 0.0})
    full_path = os.path.abspath(os.path.join(config.MOVIES_DIR, filename)).replace("\\", "/")
    entry = library.get_resume_entry(filename, fallback_keys=[full_path, os.path.basename(filename)])
    return jsonify({
        "time": float(entry.get("position", 0.0) or 0.0),
        "sub_delay": float(entry.get("sub_delay", 0.0) or 0.0),
    })


@app.route("/api/resume_save", methods=["POST"])
def resume_save():
    body = request.get_json(silent=True) or {}
    filename = (body.get("file") or "").strip()
    time_val = float(body.get("time", 0) or 0.0)
    if not filename:
        return "bad request", 400
    library.update_resume_entry(filename, position=time_val)
    return "ok"


@app.route("/stream/<path:filepath>")
def stream_video(filepath: str):
    """Serve video with HTTP 206 byte-range support and 512 KB chunks for 4K remuxes."""
    full_path = library.resolve_safe_media_path(filepath)
    if not full_path:
        abort(404)

    file_size = os.path.getsize(full_path)
    range_header = request.headers.get("Range", None)
    mime_type, _ = mimetypes.guess_type(full_path)
    if not mime_type:
        ext = os.path.splitext(full_path)[1].lower()
        mime_type = "video/x-matroska" if ext == ".mkv" else "video/mp4"

    chunk_size = config.STREAM_CHUNK_SIZE

    if range_header:
        m = re.search(r"bytes=(\d+)-(\d*)", range_header)
        if m:
            byte1 = int(m.group(1))
            byte2 = int(m.group(2)) if m.group(2) else file_size - 1
        else:
            byte1, byte2 = 0, file_size - 1

        if byte1 >= file_size:
            return Response(status=416, headers={"Content-Range": f"bytes */{file_size}"})

        byte2 = min(byte2, file_size - 1)
        length = byte2 - byte1 + 1

        def generate_range() -> Generator[bytes, None, None]:
            with open(full_path, "rb") as f:
                f.seek(byte1)
                remaining = length
                while remaining > 0:
                    data = f.read(min(chunk_size, remaining))
                    if not data:
                        break
                    yield data
                    remaining -= len(data)

        rv = Response(generate_range(), 206, mimetype=mime_type, direct_passthrough=True)
        rv.headers.add("Content-Range", f"bytes {byte1}-{byte2}/{file_size}")
        rv.headers.add("Accept-Ranges", "bytes")
        rv.headers.add("Content-Length", str(length))
        return rv

    def generate_full() -> Generator[bytes, None, None]:
        with open(full_path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    rv = Response(generate_full(), 200, mimetype=mime_type, direct_passthrough=True)
    rv.headers.add("Accept-Ranges", "bytes")
    rv.headers.add("Content-Length", str(file_size))
    return rv


@app.route("/stream-compat/<path:filepath>")
def stream_video_compatible(filepath: str):
    """Zero-video-transcode fragmented MP4 stream with stereo AAC audio for browser playback."""
    full_path = library.resolve_safe_media_path(filepath)
    if not full_path or not shutil.which("ffmpeg"):
        abort(404)

    start_sec = float(request.args.get("start", "0") or 0.0)
    raw_aidx = request.args.get("audio_track")
    if raw_aidx is not None and raw_aidx.strip() != "":
        audio_idx = int(raw_aidx)
    else:
        audio_idx = 0
        try:
            probe = media_probe.probe_file(full_path)
            auds = probe.get("audio_tracks") or []
            if len(auds) > 1 and (auds[0].get("lang_code") or "") not in ("en", "hr"):
                for idx_i, a in enumerate(auds):
                    if (a.get("lang_code") or "") in ("en", "hr") and "commentary" not in (a.get("title") or "").lower():
                        audio_idx = idx_i
                        break
        except Exception:
            audio_idx = 0

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if start_sec > 0.5:
        cmd += ["-ss", f"{start_sec:.2f}"]
    cmd += [
        "-i", full_path,
        "-map", "0:v:0",
        "-map", f"0:a:{audio_idx}?",
        "-c:v", "copy",
        "-c:a", "aac",
        "-ac", "2",
        "-b:a", "256k",
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "pipe:1",
    ]

    def generate_remux() -> Generator[bytes, None, None]:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while True:
                if not proc.stdout:
                    break
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass

    return Response(generate_remux(), 200, mimetype="video/mp4", direct_passthrough=True)


# ─── Croatian & External Subtitles API ────────────────────────────────────────

@app.route("/api/subtitles")
def list_movie_subtitles():
    movie = _resolve_movie_from_request()
    if not movie:
        return jsonify({"error": "Movie not found"}), 404
    offset = float(request.args.get("offset", "0") or 0.0)
    tracks = []
    for t in (movie.get("subtitles") or []):
        t_copy = dict(t)
        base_url = t_copy.get("vtt_url") or t_copy.get("url") or ""
        if base_url and offset != 0.0:
            sep = "&" if "?" in base_url else "?"
            base_url = f"{base_url}{sep}offset={offset}"
        t_copy["url"] = base_url
        t_copy["vtt_url"] = base_url
        tracks.append(t_copy)
    return jsonify({
        "rel_video": movie["rel_video"],
        "has_hr_local": movie["has_hr_subs_local"],
        "has_hr_downloadable": movie["has_hr_subs_downloadable"],
        "hr_sync_confidence": movie["hr_sync_confidence"],
        "has_en_local": movie["has_en_subs_local"],
        "sub_delay": movie["sub_delay"],
        "subtitles": tracks,
        "online_subtitles": movie["online_subtitles"],
    })


@app.route("/subtitles/<path:filepath>")
def serve_subtitle_vtt(filepath: str):
    """Serve any local sidecar subtitle (.srt/.ass/.sub/.vtt) as UTF-8 WebVTT with cp1250 support and live offset."""
    full_path = library.resolve_safe_media_path(filepath)
    if not full_path:
        abort(404)

    offset = float(request.args.get("offset", "0") or 0.0)
    try:
        vtt_content, detected_enc = subtitles.convert_subtitle_file_to_vtt(full_path, offset_seconds=offset)
        resp = Response(vtt_content, mimetype="text/vtt; charset=utf-8")
        resp.headers["X-Subtitle-Encoding"] = detected_enc
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as e:
        return Response(f"Error reading subtitle: {e}", status=500)


@app.route("/subtitles-embedded/<path:filepath>")
def serve_embedded_subtitle_vtt(filepath: str):
    """Extract an embedded MKV/MP4 text subtitle stream to cached UTF-8 WebVTT for browser players."""
    full_path = library.resolve_safe_media_path(filepath)
    if not full_path:
        abort(404)

    stream_index = int(request.args.get("stream_index") or request.args.get("stream") or "0")
    offset = float(request.args.get("offset", "0") or 0.0)
    vtt_content = subtitles.extract_embedded_subtitle_to_vtt(
        video_path=full_path,
        stream_index=stream_index,
        offset_seconds=offset,
    )
    if not vtt_content:
        abort(404)
    resp = Response(vtt_content, mimetype="text/vtt; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/subtitles/search")
def search_external_subtitles():
    movie = _resolve_movie_from_request()
    if not movie:
        return jsonify({"error": "Movie not found"}), 404

    langs_param = (request.args.get("lang") or request.args.get("langs") or "hr,en").strip()
    langs = [l.strip().lower() for l in langs_param.split(",") if l.strip()] or ["hr", "en"]
    custom_query = request.args.get("q", "").strip()

    check = library.check_online_subtitles_for_movie(
        video_path=movie["video_path"],
        rel_video=movie["rel_video"],
        title=custom_query or movie.get("title") or "",
        year=movie.get("year"),
        imdb_id=movie.get("imdb_id") or "",
        tmdb_id=movie.get("tmdb_id"),
        languages=langs,
    )
    return jsonify(check["search_response"])


@app.route("/api/subtitles/download", methods=["POST"])
def download_external_subtitle():
    data = request.get_json(silent=True) or {}
    movie = _resolve_movie_from_request(data)
    if not movie:
        return jsonify({"error": "Movie not found"}), 404

    candidate = data.get("candidate") or data
    try:
        saved = subtitles.download_and_save_subtitle(movie["video_path"], candidate)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if _current_movie_rel == movie["rel_video"] and player.is_running():
        player.add_subtitle_file(saved["path"], select=True)

    updated_movie = library.find_movie(rel_video=movie["rel_video"])
    return jsonify({
        "ok": True,
        "status": "downloaded",
        "saved": saved,
        "subtitles": updated_movie.get("subtitles") if updated_movie else [],
        "has_hr_subs_local": updated_movie.get("has_hr_subs_local") if updated_movie else True,
    })


@app.route("/api/subtitles/upload", methods=["POST"])
def upload_subtitle():
    movie = _resolve_movie_from_request(request.form.to_dict())
    if not movie:
        return jsonify({"error": "Movie not found"}), 404

    up_file = request.files.get("file") or request.files.get("subtitle")
    if not up_file:
        return jsonify({"error": "No subtitle file uploaded"}), 400

    fname = up_file.filename or "subtitle.srt"
    lang = (request.form.get("lang") or "hr").strip().lower()
    raw_bytes = up_file.read()
    if not raw_bytes:
        return jsonify({"error": "Uploaded file is empty"}), 400

    try:
        saved = subtitles.save_uploaded_subtitle(
            video_path=movie["video_path"],
            filename=fname,
            raw_bytes=raw_bytes,
            lang=lang,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if _current_movie_rel == movie["rel_video"] and player.is_running():
        player.add_subtitle_file(saved["path"], select=True)

    updated_movie = library.find_movie(rel_video=movie["rel_video"])
    return jsonify({
        "ok": True,
        "status": "uploaded",
        "saved": saved,
        "subtitles": updated_movie.get("subtitles") if updated_movie else [],
    })


# ─── Decoupled Media Acquisition Endpoints (/api/acquisition & /api/torrent*) ─

@app.route("/api/acquisition/storage")
def acquisition_storage():
    try:
        target_dir = config.MOVIES_DIR if os.path.isdir(config.MOVIES_DIR) else "/"
        usage = shutil.disk_usage(target_dir)
        return jsonify({
            "path": config.MOVIES_DIR,
            "total_gb": round(usage.total / (1024 ** 3), 1),
            "used_gb": round(usage.used / (1024 ** 3), 1),
            "free_gb": round(usage.free / (1024 ** 3), 1),
            "warning": config.storage_problem(),
        })
    except OSError as e:
        return jsonify({"path": config.MOVIES_DIR, "error": str(e)})


@app.route("/api/acquisition/search")
@app.route("/api/torrents/search")
@app.route("/api/torrent/search")
def search_acquisition():
    query = request.args.get("q", "").strip()
    mode = request.args.get("mode", "all").strip().lower()
    if not query:
        return jsonify({"error": "No query provided"}), 400

    def _float_arg(*names: str) -> Optional[float]:
        for n in names:
            raw = request.args.get(n, "").strip()
            if raw:
                try:
                    return float(raw)
                except ValueError:
                    pass
        return None

    min_gb = _float_arg("min_gb", "min_size")
    max_gb = _float_arg("max_gb", "max_size")
    resolution = (request.args.get("resolution") or request.args.get("quality") or "").strip() or None
    sort_by = (request.args.get("sort") or request.args.get("sort_by") or "seeds").strip()

    res = acquisition.search_all_sources(
        query,
        mode=mode,
        min_gb=min_gb,
        max_gb=max_gb,
        resolution=resolution,
        sort_by=sort_by,
    )
    if request.path.startswith("/api/torrent/") and request.args.get("format") != "v2":
        return jsonify({"data": res.get("movies", []), "releases": res.get("releases", [])})

    return jsonify(res)


@app.route("/api/acquisition/download", methods=["POST"])
@app.route("/api/torrents/download", methods=["POST"])
@app.route("/api/torrent/download", methods=["POST"])
def start_acquisition_download():
    data = request.get_json(silent=True) or {}
    magnet_or_hash = data.get("magnet") or data.get("hash") or ""
    title = data.get("title") or ""
    poster_url = data.get("poster_url") or data.get("poster") or ""

    ok, msg, tid = acquisition.start_download(magnet_or_hash, title, poster_url=poster_url)
    if not ok:
        return jsonify({"error": msg, "id": tid}), 400
    return jsonify({"status": "started", "message": msg, "id": tid, "title": title})


@app.route("/api/acquisition/status")
@app.route("/api/torrents/status")
@app.route("/api/torrent/status")
def acquisition_status():
    status_map = acquisition.get_download_status()
    if request.path.startswith("/api/torrent/"):
        return jsonify(list(status_map.values()))
    return jsonify(status_map)


@app.route("/api/acquisition/action", methods=["POST"])
@app.route("/api/torrents/cancel", methods=["POST"])
@app.route("/api/torrent/cancel", methods=["POST"])
def acquisition_action():
    data = request.get_json(silent=True) or {}
    tid = str(data.get("id") or data.get("hash") or "")
    action = data.get("action") or ("cancel" if request.path.endswith("/cancel") else "")

    if action == "pause":
        ok = acquisition.pause_download(tid)
        return jsonify({"status": "paused" if ok else "failed"})
    if action == "resume":
        ok = acquisition.resume_download(tid)
        return jsonify({"status": "resumed" if ok else "failed"})
    if action == "clear":
        removed = acquisition.clear_finished()
        return jsonify({"status": "cleared", "removed": removed})
    if action == "cancel":
        ok = acquisition.cancel_download(tid, delete_files=bool(data.get("delete_files", True)))
        if ok:
            return jsonify({"status": "cancelled", "cancelled": True})
        return jsonify({"error": "Download not found", "cancelled": False}), 404

    return jsonify({"error": "Unknown action"}), 400


if __name__ == "__main__":
    print(f"  movies dir    : {config.MOVIES_DIR}")
    print(f"  player        : {config.MPV_EXE} (ipc {config.MPV_IPC_PATH})")
    print(f"  listening on  : http://{config.HOST}:{config.PORT}\n")
    app.run(host=config.HOST, port=config.PORT, debug=False, threaded=True)
