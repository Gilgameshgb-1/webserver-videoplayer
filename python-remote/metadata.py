"""Movie metadata and poster art service (TMDb + keyless fallbacks + offline cache).

Decouples movie metadata from torrent acquisition:
  1. TMDb API v3 (`api.themoviedb.org/3`) when `TMDB_API_KEY` or
     `TMDB_BEARER_TOKEN` is set — provides canonical titles, IMDb ID, TMDb ID,
     high-res posters, synopses, genres, runtime, director, and cast.
  2. Keyless public fallbacks (iTunes Movie Search API + YTS Movie Catalog API)
     when no TMDb key is configured, so out-of-the-box installs still get
     posters, IMDb IDs, ratings, genres, and synopses.
  3. Local `.nfo` parsing (extracts IMDb `tt1234567` IDs and XML `<plot>`,
     `<title>`, `<year>`, `<rating>` tags from Kodi/Jellyfin/scene NFOs).
  4. Persistent disk cache (`STATE_DIR/posters/<slug>.json` and `<slug>.jpg`)
     so the library renders offline when the Pi broadcasts its own hotspot.

Metadata is never a hard dependency for scanning or playing a movie.
"""

import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET

import requests

import config
import release_parser

_lock = threading.Lock()
_cache = None
_last_call = [0.0]
_MIN_INTERVAL = 0.35

TMDB_BASE = 'https://api.themoviedb.org/3'
TMDB_IMG_BASE = 'https://image.tmdb.org/t/p/w780'


def _load_cache():
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(config.META_CACHE_FILE, 'r', encoding='utf-8') as f:
            _cache = json.load(f)
    except (OSError, ValueError):
        _cache = {}
    return _cache


def _save_cache():
    config.ensure_dirs()
    tmp = config.META_CACHE_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_cache or {}, f, indent=1)
        os.replace(tmp, config.META_CACHE_FILE)
    except OSError:
        pass


def _cache_key(title, year=None):
    clean = re.sub(r'[^a-z0-9]+', '', (title or '').lower())
    return f'{clean}:{year or ""}'


def _throttle():
    with _lock:
        wait = _MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def parse_local_nfo(folder_path):
    """Inspect any `.nfo` file in `folder_path` for IMDb IDs or Kodi XML metadata."""
    if not folder_path or not os.path.isdir(folder_path):
        return {}
    try:
        nfo_files = [
            os.path.join(folder_path, f)
            for f in sorted(os.listdir(folder_path))
            if f.lower().endswith('.nfo')
        ]
    except OSError:
        return {}

    info = {}
    for nfo in nfo_files:
        try:
            with open(nfo, 'rb') as f:
                raw = f.read(65536).decode('utf-8', errors='replace')
        except OSError:
            continue

        m_imdb = re.search(r'(tt\d{7,9})', raw)
        if m_imdb:
            info['imdb_id'] = m_imdb.group(1)

        if '<movie' in raw.lower():
            try:
                root = ET.fromstring(raw)
                if root.findtext('title'):
                    info['title'] = root.findtext('title').strip()
                if root.findtext('year') and root.findtext('year').strip().isdigit():
                    info['year'] = int(root.findtext('year').strip())
                if root.findtext('plot'):
                    info['synopsis'] = root.findtext('plot').strip()
                if root.findtext('runtime') and root.findtext('runtime').strip().isdigit():
                    info['runtime'] = int(root.findtext('runtime').strip())
                if root.findtext('rating'):
                    try:
                        info['rating'] = round(float(root.findtext('rating').strip()), 1)
                    except ValueError:
                        pass
                genres = [g.text.strip() for g in root.findall('genre') if g.text and g.text.strip()]
                if genres:
                    info['genres'] = genres
                if root.findtext('director'):
                    info['director'] = root.findtext('director').strip()
            except ET.ParseError:
                pass
    return info


def _tmdb_headers_and_params():
    headers = {'Accept': 'application/json', 'User-Agent': 'my-retro-os/stream-pi'}
    params = {}
    if config.TMDB_BEARER_TOKEN:
        headers['Authorization'] = f'Bearer {config.TMDB_BEARER_TOKEN}'
    elif config.TMDB_API_KEY:
        params['api_key'] = config.TMDB_API_KEY
    return headers, params


def tmdb_configured():
    return bool(config.TMDB_API_KEY or config.TMDB_BEARER_TOKEN)


def _lookup_tmdb(title, year=None):
    """Query TMDb API v3 for `title` (and optional `year`)."""
    if not tmdb_configured():
        return None

    headers, base_params = _tmdb_headers_and_params()
    search_params = dict(base_params, query=title, include_adult='false')
    if year:
        search_params['year'] = int(year)

    _throttle()
    resp = requests.get(f'{TMDB_BASE}/search/movie', headers=headers, params=search_params, timeout=12)
    resp.raise_for_status()
    results = (resp.json() or {}).get('results') or []
    if not results and year:
        # Retry without year restriction if release year differed by 1 year (festival vs wide release)
        search_params.pop('year', None)
        resp = requests.get(f'{TMDB_BASE}/search/movie', headers=headers, params=search_params, timeout=12)
        resp.raise_for_status()
        results = (resp.json() or {}).get('results') or []

    if not results:
        return None

    movie_id = results[0].get('id')
    detail_params = dict(base_params, append_to_response='credits,external_ids')
    resp_det = requests.get(f'{TMDB_BASE}/movie/{movie_id}', headers=headers, params=detail_params, timeout=12)
    resp_det.raise_for_status()
    det = resp_det.json() or {}

    release_date = det.get('release_date') or results[0].get('release_date') or ''
    parsed_year = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else year
    genres = [g.get('name') for g in (det.get('genres') or []) if g.get('name')]

    credits = det.get('credits') or {}
    directors = [
        c.get('name') for c in (credits.get('crew') or [])
        if c.get('job') == 'Director' and c.get('name')
    ]
    cast = [
        c.get('name') for c in (credits.get('cast') or [])[:4]
        if c.get('name')
    ]
    poster_path = det.get('poster_path') or results[0].get('poster_path')
    backdrop_path = det.get('backdrop_path') or results[0].get('backdrop_path')
    vote = det.get('vote_average') or results[0].get('vote_average')

    return {
        'provider':       'tmdb',
        'tmdb_id':        movie_id,
        'imdb_id':        det.get('imdb_id') or (det.get('external_ids') or {}).get('imdb_id'),
        'title':          det.get('title') or results[0].get('title') or title,
        'original_title': det.get('original_title') or '',
        'year':           parsed_year,
        'runtime':        det.get('runtime') or 0,
        'rating':         round(float(vote), 1) if vote else None,
        'genres':         genres,
        'synopsis':       (det.get('overview') or results[0].get('overview') or '').strip(),
        'tagline':        (det.get('tagline') or '').strip(),
        'director':       ', '.join(directors[:2]),
        'cast':           cast,
        'poster':         f'{TMDB_IMG_BASE}{poster_path}' if poster_path else '',
        'backdrop':       f'{TMDB_IMG_BASE}{backdrop_path}' if backdrop_path else '',
    }


def _lookup_yts_catalog(title, year=None):
    """Keyless fallback metadata lookup via the YTS movie metadata endpoint."""
    _throttle()
    resp = requests.get(
        'https://movies-api.accel.li/api/v2/list_movies.json',
        params={'query_term': title, 'limit': 5},
        headers={'User-Agent': 'my-retro-os/stream-pi'},
        timeout=10,
    )
    resp.raise_for_status()
    movies = ((resp.json() or {}).get('data') or {}).get('movies') or []
    if not movies:
        return None

    chosen = movies[0]
    if year:
        for m in movies:
            if m.get('year') and abs(int(m['year']) - int(year)) <= 1:
                chosen = m
                break

    poster = chosen.get('large_cover_image') or chosen.get('medium_cover_image') or ''
    return {
        'provider':       'yts-catalog',
        'tmdb_id':        None,
        'imdb_id':        chosen.get('imdb_code') or None,
        'title':          chosen.get('title_english') or chosen.get('title') or title,
        'original_title': chosen.get('title') or '',
        'year':           chosen.get('year') or year,
        'runtime':        chosen.get('runtime') or 0,
        'rating':         chosen.get('rating') or None,
        'genres':         chosen.get('genres') or [],
        'synopsis':       (chosen.get('description_full') or chosen.get('synopsis') or '').strip(),
        'tagline':        '',
        'director':       '',
        'cast':           [],
        'poster':         poster,
        'backdrop':       chosen.get('background_image_original') or chosen.get('background_image') or '',
    }


def _lookup_itunes_catalog(title, year=None):
    """Keyless fallback metadata lookup via Apple iTunes Search API."""
    _throttle()
    resp = requests.get(
        'https://itunes.apple.com/search',
        params={'term': title, 'media': 'movie', 'entity': 'movie', 'limit': 5},
        headers={'User-Agent': 'my-retro-os/stream-pi'},
        timeout=10,
    )
    resp.raise_for_status()
    results = (resp.json() or {}).get('results') or []
    if not results:
        return None

    chosen = results[0]
    if year:
        for r in results:
            rel = r.get('releaseDate') or ''
            if len(rel) >= 4 and rel[:4].isdigit() and abs(int(rel[:4]) - int(year)) <= 1:
                chosen = r
                break

    rel_date = chosen.get('releaseDate') or ''
    parsed_year = int(rel_date[:4]) if len(rel_date) >= 4 and rel_date[:4].isdigit() else year
    art = chosen.get('artworkUrl100') or ''
    if art:
        art = re.sub(r'\d+x\d+bb', '600x900bb', art)
    time_ms = chosen.get('trackTimeMillis') or 0

    return {
        'provider':       'itunes',
        'tmdb_id':        None,
        'imdb_id':        None,
        'title':          chosen.get('trackName') or title,
        'original_title': chosen.get('trackName') or '',
        'year':           parsed_year,
        'runtime':        int(round(time_ms / 60000)) if time_ms else 0,
        'rating':         None,
        'genres':         [chosen.get('primaryGenreName')] if chosen.get('primaryGenreName') else [],
        'synopsis':       (chosen.get('longDescription') or chosen.get('shortDescription') or '').strip(),
        'tagline':        '',
        'director':       chosen.get('artistName') or '',
        'cast':           [],
        'poster':         art,
        'backdrop':       '',
    }


def lookup(raw_title, year=None, folder_path=None, use_cache=True):
    """Look up movie metadata by raw folder/file title, with local NFO & disk caching."""
    parsed = release_parser.parse(raw_title or '')
    clean_title = parsed['title'] or raw_title
    year = year or parsed['year']

    nfo_info = parse_local_nfo(folder_path) if folder_path else {}
    if nfo_info.get('title'):
        clean_title = nfo_info['title']
    if nfo_info.get('year') and not year:
        year = nfo_info['year']

    key = _cache_key(clean_title, year)
    if not key or key == ':':
        return nfo_info or None

    cache = _load_cache()
    if use_cache and key in cache and cache[key]:
        cached = dict(cache[key])
        for k, v in nfo_info.items():
            if v and not cached.get(k):
                cached[k] = v
        return cached

    info = None
    # 1. Try TMDb first if configured
    if tmdb_configured():
        try:
            info = _lookup_tmdb(clean_title, year=year)
        except Exception:
            info = None

    # 2. Try YTS catalog (keyless, includes IMDb ID + rating + synopsis + poster)
    if not info:
        try:
            info = _lookup_yts_catalog(clean_title, year=year)
        except Exception:
            info = None

    # 3. Try iTunes Search API (keyless, high-res artwork + director + runtime + synopsis)
    if not info or not info.get('synopsis') or not info.get('poster'):
        try:
            it_info = _lookup_itunes_catalog(clean_title, year=year)
            if it_info:
                if not info:
                    info = it_info
                else:
                    for field in ('poster', 'synopsis', 'runtime', 'director', 'year'):
                        if not info.get(field) and it_info.get(field):
                            info[field] = it_info[field]
        except Exception:
            pass

    if info:
        for k, v in nfo_info.items():
            if v and not info.get(k):
                info[k] = v
        cache[key] = info
        _save_cache()
        return info

    if nfo_info:
        return nfo_info
    return None


def fetch_poster(url, dest_path, timeout=20):
    """Download poster image from `url` to `dest_path`. Returns True on success."""
    if not url:
        return False
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        resp = requests.get(
            url,
            headers={'User-Agent': 'my-retro-os/stream-pi'},
            timeout=timeout,
        )
        resp.raise_for_status()
        tmp = dest_path + '.part'
        with open(tmp, 'wb') as f:
            f.write(resp.content)
        os.replace(tmp, dest_path)
        return True
    except (requests.RequestException, OSError):
        return False


def get_movie_metadata(video_path, folder_name='', release_info=None, fetch_online=False, force_refresh=False):
    """Return normalized metadata dictionary for `library.py` (offline-first unless `fetch_online=True`)."""
    folder_path = os.path.dirname(video_path) if video_path else None
    rel = release_info or release_parser.parse_release_name(folder_name or os.path.basename(video_path or ''))
    clean_title = rel.get('clean_title') or rel.get('title') or folder_name
    year = rel.get('year')

    nfo_info = parse_local_nfo(folder_path) if folder_path else {}
    if nfo_info.get('title'):
        clean_title = nfo_info['title']
    if nfo_info.get('year') and not year:
        year = nfo_info['year']

    key = _cache_key(clean_title, year)
    cache = _load_cache()
    info = None

    if not force_refresh and key in cache and cache[key]:
        info = dict(cache[key])
    elif fetch_online or force_refresh:
        info = lookup(clean_title, year=year, folder_path=folder_path, use_cache=not force_refresh)

    merged = dict(nfo_info)
    if info:
        merged.update({k: v for k, v in info.items() if v is not None})

    # Check or cache local poster image
    poster_url = merged.get('poster') or ''
    if key and poster_url and poster_url.startswith('http'):
        slug_safe = re.sub(r'[^a-z0-9_-]+', '_', key)
        local_poster = os.path.join(config.POSTERS_DIR, f'{slug_safe}.jpg')
        if os.path.isfile(local_poster):
            poster_url = f'/poster/{slug_safe}.jpg'
        elif fetch_online or force_refresh:
            if fetch_poster(poster_url, local_poster):
                poster_url = f'/poster/{slug_safe}.jpg'

    director_str = merged.get('director') or ''
    directors = [d.strip() for d in director_str.split(',') if d.strip()] if isinstance(director_str, str) else list(director_str)

    return {
        'title':           merged.get('title') or clean_title,
        'original_title':  merged.get('original_title') or '',
        'year':            merged.get('year') or year,
        'overview':        merged.get('synopsis') or merged.get('overview') or '',
        'tagline':         merged.get('tagline') or '',
        'genres':          merged.get('genres') or [],
        'rating':          merged.get('rating'),
        'runtime_minutes': merged.get('runtime') or None,
        'directors':       directors,
        'cast':            merged.get('cast') or [],
        'poster_url':      poster_url or None,
        'backdrop_url':    merged.get('backdrop') or None,
        'imdb_id':         merged.get('imdb_id') or '',
        'tmdb_id':         merged.get('tmdb_id'),
        'metadata_source': merged.get('provider') or ('nfo' if nfo_info else 'filename'),
    }

