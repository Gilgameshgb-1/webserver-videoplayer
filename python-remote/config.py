"""Runtime configuration for Huberry Stream (stream.pi).

Supports both:
- Local Linux/Windows desktop development & testing (`~/movies`, `/tmp/mpv-socket`,
  local `build/wserVideoPlayer` or system `mpv` binary)
- Raspberry Pi 5 Yocto appliance (`/movies` NVMe mount, `/usr/bin/wserVideoPlayer`,
  `/tmp/mpv-socket`)
"""

import os
import platform
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))


def _env(name, default=''):
    return os.environ.get(name, default)


ON_PI = (
    _env('STREAM_TARGET', '') == 'pi'
    or platform.machine() in ('aarch64', 'armv7l')
)

# ─── Media & state paths ──────────────────────────────────────────────────
if platform.system() == 'Windows':
    _default_movies = r'C:\webserver-videoplayer\movies'
    _default_state = os.path.join(_HERE, '.state')
elif ON_PI:
    _default_movies = '/movies' if os.path.isdir('/movies') else '/home/root/movies'
    _default_state = '/home/root/.stream-pi'
else:
    _home_movies = os.path.expanduser('~/movies')
    _default_movies = _home_movies if os.path.isdir(_home_movies) else os.path.join(_HERE, 'movies')
    _default_state = os.path.join(_HERE, '.state')

MOVIES_DIR = os.path.abspath(_env('MOVIES_DIR', _default_movies))

# Optional colon-separated extra directories to scan for movies
_extra_raw = _env('EXTRA_MOVIES_DIRS', '/home/root/movies' if ON_PI else '')
EXTRA_MOVIES_DIRS = [
    os.path.abspath(p)
    for p in _extra_raw.split(':')
    if p.strip() and os.path.abspath(p) != MOVIES_DIR and os.path.isdir(p)
]

# Top-level directory names inside MOVIES_DIR that belong to other services
IGNORED_DIRS = {'anime', 'lost+found', '.state', '.stream-pi', '.Trash-0', '.Trash-1000', '__pycache__'}

STATE_DIR          = os.path.abspath(_env('STREAM_STATE_DIR', _default_state))
ASSETS_DIR         = os.path.join(_HERE, 'assets')
RESUME_FILE        = os.path.join(STATE_DIR, 'resume.json')
LEGACY_RESUME_FILE = os.path.join(_HERE, 'resume_data.json')
META_CACHE_FILE    = os.path.join(STATE_DIR, 'metadata-cache.json')
PROBE_CACHE_FILE   = os.path.join(STATE_DIR, 'probe-cache.json')
SUB_INDEX_FILE     = os.path.join(STATE_DIR, 'subtitles-index.json')
EXTRACTED_SUBS_DIR = os.path.join(STATE_DIR, 'extracted-subs')
POSTERS_DIR        = os.path.join(STATE_DIR, 'posters')
POSTER_CACHE_DIR   = POSTERS_DIR
STREAM_CHUNK_SIZE  = 512 * 1024  # 512 KB range chunks for 4K remux streaming

# ─── Player (mpv / wserVideoPlayer) ───────────────────────────────────────
HEADLESS = _env('STREAM_HEADLESS', '') in ('1', 'true', 'yes')
_custom_sock = _env('MPV_IPC_PATH', '') or _env('MPV_SOCKET', '')

if platform.system() == 'Windows':
    MPV_IPC_PATH = _custom_sock or r'\\.\pipe\mpv-pipe'
else:
    MPV_IPC_PATH = _custom_sock or '/tmp/mpv-socket'

_local_built_player = os.path.normpath(
    os.path.join(_HERE, '..', 'build', 'wserVideoPlayer.exe' if platform.system() == 'Windows' else 'wserVideoPlayer')
)

if ON_PI and not HEADLESS and os.path.isfile('/usr/bin/wserVideoPlayer'):
    MPV_EXE = _env('MPV_EXE', '/usr/bin/wserVideoPlayer')
    MPV_ARGS = []
elif not HEADLESS and os.path.isfile(_local_built_player) and os.access(_local_built_player, os.X_OK):
    MPV_EXE = _env('MPV_EXE', _local_built_player)
    MPV_ARGS = []
else:
    MPV_EXE = _env('MPV_EXE', shutil.which('mpv') or 'mpv')
    if HEADLESS:
        MPV_ARGS = [
            '--idle=yes',
            '--vo=null',
            '--ao=null',
            '--keep-open=yes',
            '--sub-codepage=utf-8:cp1250',
            f'--input-ipc-server={MPV_IPC_PATH}',
        ]
    else:
        MPV_ARGS = [
            '--idle=yes',
            '--force-window=yes',
            '--keep-open=yes',
            '--cache=yes',
            '--demuxer-max-bytes=128MiB',
            '--demuxer-max-back-bytes=32MiB',
            '--hdr-compute-peak=no',
            '--tone-mapping=bt.2390',
            '--audio-channels=auto-safe',
            '--ad-lavc-downmix=yes',
            '--sub-codepage=utf-8:cp1250',
            f'--input-ipc-server={MPV_IPC_PATH}',
        ]

PLAYER_BINARY = MPV_EXE

# ─── Web server ───────────────────────────────────────────────────────────
HOST = _env('FLASK_HOST', '0.0.0.0')
PORT = int(_env('FLASK_PORT', '5001' if ON_PI else '5000'))

# ─── Storage & download guards ────────────────────────────────────────────
WARN_SIZE_BYTES = int(_env('STREAM_WARN_SIZE_GB', '35')) * (1024 ** 3)
REQUIRE_MOUNT   = _env('STREAM_REQUIRE_MOUNT', '')

# ─── Optional external API credentials ────────────────────────────────────
TMDB_API_KEY          = _env('TMDB_API_KEY', '')
TMDB_BEARER_TOKEN     = _env('TMDB_BEARER_TOKEN', '')
OPENSUBTITLES_API_KEY = _env('OPENSUBTITLES_API_KEY', '')
SUBDL_API_KEY         = _env('SUBDL_API_KEY', '')

VIDEO_EXTS = ('.mkv', '.mp4', '.m4v', '.mov', '.webm', '.avi', '.m2ts', '.ts')
SUB_EXTS   = ('.srt', '.ass', '.ssa', '.vtt', '.sub')


def storage_problem():
    """Return a human-readable error if the target mount is missing, else None."""
    if REQUIRE_MOUNT and not os.path.ismount(REQUIRE_MOUNT):
        return (
            f'The NVMe SSD is not mounted at {REQUIRE_MOUNT}, so downloads '
            'are paused to avoid filling the SD card.'
        )
    return None


def ensure_dirs():
    if not storage_problem():
        os.makedirs(MOVIES_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(EXTRACTED_SUBS_DIR, exist_ok=True)
    os.makedirs(POSTERS_DIR, exist_ok=True)


ensure_dirs()
