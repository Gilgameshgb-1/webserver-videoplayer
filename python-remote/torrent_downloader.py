import libtorrent as lt
import requests
import threading
import shutil
import glob
import time
import os

YTS_API_BASE = "https://movies-api.accel.li/api/v2"

YTS_TRACKERS = [
    "udp://glotorrents.pw:6969/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://torrent.gresille.org:80/announce",
    "udp://tracker.openbittorrent.com:80",
    "udp://tracker.coppersurfer.tk:6969",
    "udp://tracker.leechers-paradise.org:6969",
    "udp://p4p.arenabg.ch:1337",
    "udp://tracker.internetwarriors.net:1337",
]

active_downloads = {}
_download_counter = 0
_lock = threading.Lock()

def _build_magnet(info_hash, title):
    params = f"magnet:?xt=urn:btih:{info_hash}"
    params += f"&dn={requests.utils.quote(title)}"
    for tr in YTS_TRACKERS:
        params += f"&tr={requests.utils.quote(tr)}"
    return params


def search_yts(query, limit=20, page=1):
    resp = requests.get(
        f"{YTS_API_BASE}/list_movies.json",
        params={"query_term": query, "limit": limit, "page": page},
        timeout=15,
    )
    movies = resp.json().get("data", {}).get("movies", []) or []

    data = []
    for m in movies:
        torrents = []
        for t in m.get("torrents", []):
            torrents.append({
                "quality": t.get("quality", ""),
                "type": t.get("type", ""),
                "size": t.get("size", ""),
                "seeds": t.get("seeds", 0),
                "peers": t.get("peers", 0),
                "magnet": _build_magnet(t.get("hash", ""), m.get("title_long", m.get("title", ""))),
            })
        data.append({
            "name": m.get("title", "Unknown"),
            "year": m.get("year", ""),
            "genre": m.get("genres", []),
            "poster": m.get("medium_cover_image", ""),
            "rating": m.get("rating", ""),
            "synopsis": m.get("description_full", ""),
            "torrents": torrents,
        })
    return {"data": data}


def _download_poster(poster_url, save_path, title):
    if not poster_url:
        return
    poster_url = poster_url.replace("medium-cover", "large-cover")
    resp = requests.get(poster_url, timeout=15)
    with open(os.path.join(save_path, f"{title}.jpg"), "wb") as f:
        f.write(resp.content)


def _cleanup_yts_images(save_path):
    yts_patterns = ["*YTS*", "*yts*", "*YIFY*", "*yify*"]
    for pattern in yts_patterns:
        for f in glob.glob(os.path.join(save_path, pattern)):
            if f.lower().endswith((".jpg", ".png", ".gif", ".bmp", ".webp")):
                os.remove(f)


def _flatten_torrent_files(save_path):
    for root, dirs, files in os.walk(save_path, topdown=False):
        if root == save_path:
            continue
        for name in files:
            src = os.path.join(root, name)
            dst = os.path.join(save_path, name)
            if not os.path.exists(dst):
                shutil.move(src, dst)
        os.rmdir(root)


_INVALID_CHARS = r'\/:*?"<>|'

def _safe_dirname(title):
    for ch in _INVALID_CHARS:
        title = title.replace(ch, '-')
    return title.strip(' .-')

def start_download(magnet_link, title, movies_dir, poster_url=None):
    global _download_counter

    save_path = os.path.join(movies_dir, _safe_dirname(title))
    os.makedirs(save_path, exist_ok=True)

    with _lock:
        _download_counter += 1
        dl_id = _download_counter

    active_downloads[dl_id] = {
        "id": dl_id,
        "title": title,
        "save_path": save_path,
        "poster_url": poster_url,
        "progress": 0.0,
        "download_rate": 0,
        "upload_rate": 0,
        "num_peers": 0,
        "state": "starting",
        "error": None,
    }

    thread = threading.Thread(target=_download_worker, args=(dl_id, magnet_link, save_path, title, poster_url), daemon=True)
    thread.start()
    return dl_id


def _download_worker(dl_id, magnet_link, save_path, title, poster_url):
    try:
        # listen_on() was removed in libtorrent 2.x — use settings dict instead
        ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})

        params = lt.parse_magnet_uri(magnet_link)
        params.save_path = save_path
        handle = ses.add_torrent(params)

        active_downloads[dl_id]["state"] = "downloading"

        # download poster in parallel while torrent starts
        if poster_url:
            poster_thread = threading.Thread(target=_download_poster, args=(poster_url, save_path, title), daemon=True)
            poster_thread.start()

        while not handle.status().is_seeding:
            if dl_id not in active_downloads:
                ses.remove_torrent(handle)
                return

            s = handle.status()
            active_downloads[dl_id].update({
                "progress": round(s.progress * 100, 1),
                "download_rate": s.download_rate,
                "upload_rate": s.upload_rate,
                "num_peers": s.num_peers,
                "state": str(s.state),
            })
            time.sleep(1)

        active_downloads[dl_id].update({
            "progress": 100.0,
            "state": "finished",
            "download_rate": 0,
            "upload_rate": 0,
        })

        ses.remove_torrent(handle)

        # remove yts nonsense images and flatten any nested folders
        _flatten_torrent_files(save_path)
        _cleanup_yts_images(save_path)

    except Exception as e:
        if dl_id in active_downloads:
            active_downloads[dl_id]["state"] = f"error: {e}"


def get_download_status(dl_id=None):
    if dl_id is not None:
        return active_downloads.get(dl_id)
    return list(active_downloads.values())


def cancel_download(dl_id):
    if dl_id in active_downloads:
        info = active_downloads.pop(dl_id)
        return {"cancelled": True, "title": info["title"]}
    return {"cancelled": False, "error": "Download not found"}
