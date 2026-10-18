"""Inspect movie files with ffprobe and compute OpenSubtitles hashes.

Provides fast, disk-cached stream inspection so the library scanner and movie
detail view know the exact video resolution, HDR format (HDR10, HDR10+,
Dolby Vision, HLG), framerate, bitrate, audio tracks (TrueHD 7.1 Atmos,
DTS-HD MA, E-AC-3, etc.), and embedded subtitle tracks (Croatian, English,
text vs PGS bitmap) without guessing solely from filenames.

Falls back cleanly to `release_parser` if `ffprobe` is not installed.
"""

import json
import os
import re
import struct
import subprocess
import threading

import config
import release_parser

_cache_lock = threading.Lock()
_probe_cache = None

# ISO 639-1 / 639-2 / name normalization table with first-class Croatian support
LANGUAGE_MAP = {
    'hr': ('hr', 'Croatian'),
    'hrv': ('hr', 'Croatian'),
    'scr': ('hr', 'Croatian'),
    'cro': ('hr', 'Croatian'),
    'croatian': ('hr', 'Croatian'),
    'hrvatski': ('hr', 'Croatian'),
    'en': ('en', 'English'),
    'eng': ('en', 'English'),
    'english': ('en', 'English'),
    'bs': ('bs', 'Bosnian'),
    'bos': ('bs', 'Bosnian'),
    'bosnian': ('bs', 'Bosnian'),
    'sr': ('sr', 'Serbian'),
    'srp': ('sr', 'Serbian'),
    'scc': ('sr', 'Serbian'),
    'serbian': ('sr', 'Serbian'),
    'sl': ('sl', 'Slovenian'),
    'slv': ('sl', 'Slovenian'),
    'slovenian': ('sl', 'Slovenian'),
    'de': ('de', 'German'),
    'deu': ('de', 'German'),
    'ger': ('de', 'German'),
    'german': ('de', 'German'),
    'fr': ('fr', 'French'),
    'fra': ('fr', 'French'),
    'fre': ('fr', 'French'),
    'french': ('fr', 'French'),
    'es': ('es', 'Spanish'),
    'spa': ('es', 'Spanish'),
    'spanish': ('es', 'Spanish'),
    'it': ('it', 'Italian'),
    'ita': ('it', 'Italian'),
    'italian': ('it', 'Italian'),
    'pt': ('pt', 'Portuguese'),
    'por': ('pt', 'Portuguese'),
    'ja': ('ja', 'Japanese'),
    'jpn': ('ja', 'Japanese'),
    'japanese': ('ja', 'Japanese'),
    'ko': ('ko', 'Korean'),
    'kor': ('ko', 'Korean'),
    'zh': ('zh', 'Chinese'),
    'chi': ('zh', 'Chinese'),
    'zho': ('zh', 'Chinese'),
    'ru': ('ru', 'Russian'),
    'rus': ('ru', 'Russian'),
    'pl': ('pl', 'Polish'),
    'pol': ('pl', 'Polish'),
    'cs': ('cs', 'Czech'),
    'ces': ('cs', 'Czech'),
    'cze': ('cs', 'Czech'),
    'hu': ('hu', 'Hungarian'),
    'hun': ('hu', 'Hungarian'),
    'nl': ('nl', 'Dutch'),
    'nld': ('nl', 'Dutch'),
    'dut': ('nl', 'Dutch'),
    'sv': ('sv', 'Swedish'),
    'swe': ('sv', 'Swedish'),
    'tr': ('tr', 'Turkish'),
    'tur': ('tr', 'Turkish'),
    'ar': ('ar', 'Arabic'),
    'ara': ('ar', 'Arabic'),
}


def normalize_language(lang_tag='', title=''):
    """Normalize a raw language code or track title into (iso_code, display_name)."""
    raw = (lang_tag or '').strip().lower()
    if raw in LANGUAGE_MAP:
        return LANGUAGE_MAP[raw]

    # Try stripping region subtags like en-US, hr-HR, sr-Latn
    if '-' in raw or '_' in raw:
        base = re.split(r'[-_]', raw)[0]
        if base in LANGUAGE_MAP:
            return LANGUAGE_MAP[base]

    # Inspect track title if language tag was 'und' or missing
    title_low = (title or '').strip().lower()
    for token, pair in LANGUAGE_MAP.items():
        if len(token) >= 3 and re.search(rf'\b{re.escape(token)}\b', title_low):
            return pair

    if raw and raw != 'und':
        return (raw[:2], raw.upper())
    return ('und', 'Unknown')


def compute_opensubtitles_hash(filepath):
    """Compute the 64-bit OpenSubtitles moviehash for exact subtitle sync matching.

    Hashes file size + first 64 KB + last 64 KB as little-endian uint64 values.
    For files smaller than 128 KB (such as short test clips), hashes the full
    available 8-byte blocks so provenance matching still has a deterministic hash.
    """
    try:
        filesize = os.path.getsize(filepath)
        if filesize <= 0:
            return None
        hash_val = filesize
        block_size = 65536
        with open(filepath, 'rb') as f:
            if filesize >= block_size * 2:
                buf_start = f.read(block_size)
                f.seek(max(0, filesize - block_size))
                buf_end = f.read(block_size)
                buffers = (buf_start, buf_end)
            else:
                raw = f.read()
                pad = (8 - (len(raw) % 8)) % 8
                buffers = (raw + (b'\x00' * pad),)

            for buf in buffers:
                count = len(buf) // 8
                for val in struct.unpack(f'<{count}Q', buf[:count * 8]):
                    hash_val = (hash_val + val) & 0xFFFFFFFFFFFFFFFF
        return f'{hash_val:016x}'
    except OSError:
        return None


def _load_cache():
    global _probe_cache
    if _probe_cache is not None:
        return _probe_cache
    try:
        with open(config.PROBE_CACHE_FILE, 'r', encoding='utf-8') as f:
            _probe_cache = json.load(f)
    except (OSError, ValueError):
        _probe_cache = {}
    return _probe_cache


def _save_cache():
    config.ensure_dirs()
    tmp = config.PROBE_CACHE_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_probe_cache or {}, f, indent=1)
        os.replace(tmp, config.PROBE_CACHE_FILE)
    except OSError:
        pass


def _parse_fps(rate_str):
    if not rate_str or rate_str == '0/0':
        return None
    if '/' in rate_str:
        num, den = rate_str.split('/', 1)
        try:
            n, d = float(num), float(den)
            return round(n / d, 3) if d > 0 else None
        except ValueError:
            return None
    try:
        return round(float(rate_str), 3)
    except ValueError:
        return None


def _resolution_label(width, height):
    w = int(width or 0)
    h = int(height or 0)
    if w >= 3800 or h >= 2000:
        return '2160p'
    if w >= 2500 or h >= 1400:
        return '1440p'
    if w >= 1900 or h >= 1000:
        return '1080p'
    if w >= 1200 or h >= 700:
        return '720p'
    if w > 0 or h > 0:
        return '480p'
    return None


def _video_codec_label(codec_name):
    c = (codec_name or '').lower()
    if c in ('hevc', 'h265'):
        return 'HEVC'
    if c in ('h264', 'avc', 'avc1'):
        return 'H264'
    if c == 'av1':
        return 'AV1'
    if c == 'vp9':
        return 'VP9'
    if c in ('vc1', 'vc-1'):
        return 'VC-1'
    if c in ('mpeg2video', 'mpeg2'):
        return 'MPEG-2'
    return codec_name.upper() if codec_name else None


def _audio_codec_label(codec_name, profile='', title=''):
    c = (codec_name or '').lower()
    p = (profile or '').lower()
    t = (title or '').lower()
    has_atmos = 'atmos' in p or 'atmos' in t
    if c == 'truehd':
        return 'TrueHD Atmos' if has_atmos else 'TrueHD'
    if c in ('dts', 'dca'):
        if 'ma' in p or 'master' in t or 'dts-hd ma' in t:
            return 'DTS-HD MA'
        if 'hra' in p or 'high resolution' in t:
            return 'DTS-HD HRA'
        if 'dts:x' in p or 'dts:x' in t or 'dtsx' in t:
            return 'DTS:X'
        return 'DTS'
    if c == 'eac3':
        return 'E-AC-3 Atmos' if has_atmos else 'E-AC-3'
    if c == 'ac3':
        return 'AC-3'
    if c == 'flac':
        return 'FLAC'
    if c == 'aac':
        return 'AAC'
    if c == 'opus':
        return 'Opus'
    if c == 'vorbis':
        return 'Vorbis'
    if c == 'mp3':
        return 'MP3'
    if c.startswith('pcm_'):
        return 'PCM'
    return (codec_name or 'Audio').upper()


def _channels_label(channels, layout=''):
    lay = (layout or '').lower()
    for tag in ('7.1', '5.1', '2.1', '2.0', '1.0'):
        if tag in lay:
            return tag
    ch = int(channels or 0)
    return {8: '7.1', 6: '5.1', 3: '2.1', 2: '2.0', 1: '1.0'}.get(ch, f'{ch}ch' if ch else '')


def _detect_stream_hdr(video_stream, parsed_fallback=None):
    """Inspect ffprobe color transfer, primaries, and side_data_list for HDR."""
    transfer = (video_stream.get('color_transfer') or '').lower()
    primaries = (video_stream.get('color_primaries') or '').lower()
    side_data = video_stream.get('side_data_list') or []

    has_dovi = any(
        'dovi' in (sd.get('side_data_type') or '').lower()
        or 'dolby vision' in (sd.get('side_data_type') or '').lower()
        for sd in side_data
    )
    has_hdr10plus = any(
        '2094' in (sd.get('side_data_type') or '').lower()
        for sd in side_data
    )
    has_pq = transfer == 'smpte2084' or any(
        'mastering display' in (sd.get('side_data_type') or '').lower()
        or 'content light level' in (sd.get('side_data_type') or '').lower()
        for sd in side_data
    )
    has_hlg = transfer == 'arib-std-b67'

    if has_dovi and (has_pq or has_hdr10plus):
        return 'DV / HDR10'
    if has_dovi:
        return 'Dolby Vision'
    if has_hdr10plus:
        return 'HDR10+'
    if has_pq:
        return 'HDR10'
    if has_hlg:
        return 'HLG'
    if primaries == 'bt2020' and parsed_fallback:
        return parsed_fallback
    return parsed_fallback


BITMAP_SUB_CODECS = {'hdmv_pgs_subtitle', 'pgssub', 'dvd_subtitle', 'dvdsub', 'dvb_subtitle', 'xsub'}
TEXT_SUB_CODECS   = {'subrip', 'srt', 'ass', 'ssa', 'webvtt', 'vtt', 'mov_text', 'text'}


def probe_file(abs_path, use_cache=True):
    """Return rich media probe dictionary for `abs_path`."""
    try:
        st = os.stat(abs_path)
        size = st.st_size
        mtime = int(st.st_mtime)
    except OSError:
        size = 0
        mtime = 0

    cache_key = f'v2|{abs_path}|{size}|{mtime}'
    with _cache_lock:
        cache = _load_cache()
        if use_cache and cache_key in cache:
            return cache[cache_key]

    filename = os.path.basename(abs_path)
    parsed = release_parser.parse(filename)
    moviehash = compute_opensubtitles_hash(abs_path)

    raw_probe = None
    try:
        proc = subprocess.run(
            [
                'ffprobe', '-v', 'quiet',
                '-print_format', 'json',
                '-show_format', '-show_streams', '-show_chapters',
                abs_path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raw_probe = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        raw_probe = None

    if not raw_probe:
        info = _fallback_from_parser(abs_path, size, parsed, moviehash)
        with _cache_lock:
            cache[cache_key] = info
            _save_cache()
        return info

    fmt = raw_probe.get('format') or {}
    streams = raw_probe.get('streams') or []
    chapters_raw = raw_probe.get('chapters') or []

    duration = float(fmt.get('duration') or 0)
    total_bitrate = int(fmt.get('bit_rate') or 0)

    video_streams = [s for s in streams if s.get('codec_type') == 'video'
                     and not (s.get('disposition') or {}).get('attached_pic')]
    audio_streams = [s for s in streams if s.get('codec_type') == 'audio']
    sub_streams   = [s for s in streams if s.get('codec_type') == 'subtitle']

    v0 = video_streams[0] if video_streams else {}
    width = int(v0.get('width') or 0)
    height = int(v0.get('height') or 0)
    resolution = _resolution_label(width, height) or parsed['resolution']
    codec = _video_codec_label(v0.get('codec_name')) or parsed['codec']
    pix_fmt = (v0.get('pix_fmt') or '').lower()
    profile = v0.get('profile') or ''
    ten_bit = (
        '10' in pix_fmt
        or '10' in profile
        or parsed['ten_bit']
    )
    fps = _parse_fps(v0.get('avg_frame_rate') or v0.get('r_frame_rate'))
    hdr_type = _detect_stream_hdr(v0, parsed_fallback=parsed['hdr_type'])

    v_tags = v0.get('tags') or {}
    v_bps = int(v0.get('bit_rate') or v_tags.get('BPS') or total_bitrate or 0)
    bitrate_mbps = round(v_bps / 1_000_000, 1) if v_bps > 0 else 0.0

    # Audio tracks (mpv aid is 1-based index among audio streams)
    audio_tracks = []
    for idx, a in enumerate(audio_streams, start=1):
        tags = a.get('tags') or {}
        disp = a.get('disposition') or {}
        raw_lang = tags.get('language') or ''
        title = tags.get('title') or ''
        lang_code, lang_name = normalize_language(raw_lang, title)
        a_codec = _audio_codec_label(a.get('codec_name'), a.get('profile', ''), title)
        ch_num = int(a.get('channels') or 2)
        ch_str = _channels_label(ch_num, a.get('channel_layout', ''))
        browser_ok = (
            (a.get('codec_name') or '').lower() in ('aac', 'mp3', 'opus', 'flac', 'vorbis')
            and ch_num <= 6
        )
        label_parts = [lang_name if lang_code != 'und' else '', a_codec, ch_str]
        label = ' · '.join(p for p in label_parts if p) or f'Audio {idx}'
        audio_tracks.append({
            'id':                 idx,
            'stream_index':       a.get('index', idx),
            'codec':              (a.get('codec_name') or '').lower() or a_codec,
            'codec_label':        a_codec,
            'raw_codec':          (a.get('codec_name') or '').lower(),
            'channels':           ch_num,
            'channel_str':        ch_str,
            'channel_label':      ch_str,
            'sample_rate':        int(a.get('sample_rate') or 0) if str(a.get('sample_rate') or '').isdigit() else 0,
            'lang':               lang_code if lang_code != 'und' else (raw_lang or 'und'),
            'lang_code':          lang_code,
            'lang_name':          lang_name,
            'title':              title,
            'label':              label,
            'default':            bool(disp.get('default')),
            'atmos':              'Atmos' in a_codec or 'atmos' in title.lower(),
            'lossless':           a_codec in ('TrueHD', 'TrueHD Atmos', 'DTS-HD MA', 'FLAC', 'PCM'),
            'is_lossless':        a_codec in ('TrueHD', 'TrueHD Atmos', 'DTS-HD MA', 'FLAC', 'PCM'),
            'browser_compatible': browser_ok,
        })

    # Embedded subtitle tracks (mpv sid is 1-based index among subtitle streams)
    embedded_subs = []
    for idx, s in enumerate(sub_streams, start=1):
        tags = s.get('tags') or {}
        disp = s.get('disposition') or {}
        raw_lang = tags.get('language') or ''
        title = tags.get('title') or ''
        lang_code, lang_name = normalize_language(raw_lang, title)
        s_codec = (s.get('codec_name') or 'subrip').lower()
        is_bitmap = s_codec in BITMAP_SUB_CODECS
        is_text = s_codec in TEXT_SUB_CODECS or not is_bitmap
        forced = bool(disp.get('forced')) or 'forced' in title.lower()
        sdh = bool(disp.get('hearing_impaired')) or bool(re.search(r'\bsdh\b', title, re.I))

        bits = [lang_name if lang_code != 'und' else (title or f'Track {idx}')]
        if title and title.lower() != lang_name.lower():
            bits.append(title)
        if forced and 'forced' not in ' '.join(bits).lower():
            bits.append('Forced')
        if sdh and 'sdh' not in ' '.join(bits).lower():
            bits.append('SDH')
        if is_bitmap:
            bits.append('PGS')

        embedded_subs.append({
            'id':              idx,
            'stream_index':    s.get('index'),
            'sub_index':       idx - 1,
            'codec':           s_codec,
            'lang':            lang_code,
            'lang_code':       lang_code,
            'lang_name':       lang_name,
            'title':           title,
            'label':           ' · '.join(bits),
            'forced':          forced,
            'sdh':             sdh,
            'default':         bool(disp.get('default')),
            'is_bitmap':       is_bitmap,
            'is_text':         is_text,
            'source_type':     'embedded',
            'source':          'embedded',
            'provider':        'container',
            'sync_confidence': 'embedded',
            'sync_reason':     'Muxed inside movie container (frame-accurate)',
            'release_name':    filename,
        })

    chapters = []
    for idx, ch in enumerate(chapters_raw):
        c_tags = ch.get('tags') or {}
        start_t = float(ch.get('start_time') or 0)
        end_t = float(ch.get('end_time') or 0)
        chapters.append({
            'index': idx,
            'title': c_tags.get('title') or f'Chapter {idx + 1}',
            'start': round(start_t, 2),
            'time':  round(start_t, 2),
            'end':   round(end_t, 2),
        })

    is_remux = parsed['is_remux'] or bitrate_mbps >= 35.0
    primary_audio = audio_tracks[0] if audio_tracks else None
    res_label = '4K UHD' if resolution == '2160p' else (resolution or 'Unknown')
    vid_summary_parts = [res_label if res_label != 'Unknown' else '', codec or '', hdr_type or '']
    video_summary = ' · '.join(p for p in vid_summary_parts if p)
    primary_audio_str = (
        f"{primary_audio['codec_label']} {primary_audio['channel_str']}".strip()
        if primary_audio else (parsed['audio'] or '')
    )
    size_human = (
        f"{size / (1024 ** 3):.1f} GB"
        if size >= 1024 ** 3
        else f"{size / (1024 ** 2):.1f} MB"
    )
    warn = release_parser.playback_warning(
        resolution, codec, hdr_type=hdr_type, is_remux=is_remux
    )

    info = {
        'probed':            True,
        'width':             width,
        'height':            height,
        'resolution':        resolution,
        'resolution_label':  res_label,
        'is_4k':             resolution == '2160p',
        'codec':             codec,
        'profile':           profile,
        'pix_fmt':           pix_fmt,
        'ten_bit':           ten_bit,
        'hdr':               hdr_type is not None,
        'hdr_type':          hdr_type,
        'dolby_vision':      bool(hdr_type and ('DV' in hdr_type or 'Dolby Vision' in hdr_type)),
        'fps':               fps,
        'duration':          round(duration, 2),
        'size_bytes':        size,
        'size_human':        size_human,
        'bitrate_mbps':      bitrate_mbps,
        'is_remux':          is_remux,
        'source':            parsed['source'] or ('BluRay Remux' if is_remux else None),
        'edition':           parsed['edition'],
        'group':             parsed['group'],
        'moviehash':         moviehash,
        'video_summary':     video_summary,
        'audio_summary':     primary_audio_str,
        'video_tracks':      [{
            'width': width, 'height': height, 'codec': codec,
            'hdr_type': hdr_type or 'SDR', 'fps': fps, 'ten_bit': ten_bit,
        }] if width else [],
        'audio_tracks':      audio_tracks,
        'multi_audio':       len(audio_tracks) > 1 or parsed['multi_audio'],
        'primary_audio':     primary_audio_str,
        'browser_audio_ok':  any(a['browser_compatible'] for a in audio_tracks) if audio_tracks else True,
        'embedded_subs':     embedded_subs,
        'chapters':          chapters,
        'warning':           warn,
        'playback_warnings': [warn] if warn else [],
    }


    with _cache_lock:
        cache[cache_key] = info
        _save_cache()
    return info


def _fallback_from_parser(abs_path, size, parsed, moviehash):
    return {
        'probed':           False,
        'width':            3840 if parsed['resolution'] == '2160p' else (1920 if parsed['resolution'] == '1080p' else 0),
        'height':           2160 if parsed['resolution'] == '2160p' else (1080 if parsed['resolution'] == '1080p' else 0),
        'resolution':       parsed['resolution'],
        'is_4k':            parsed['resolution'] == '2160p',
        'codec':            parsed['codec'],
        'profile':          '',
        'pix_fmt':          'yuv420p10le' if parsed['ten_bit'] else 'yuv420p',
        'ten_bit':          parsed['ten_bit'],
        'hdr':              parsed['hdr'],
        'hdr_type':         parsed['hdr_type'],
        'dolby_vision':     parsed['dolby_vision'],
        'fps':              23.976,
        'duration':         0.0,
        'size_bytes':       size,
        'bitrate_mbps':     0.0,
        'is_remux':         parsed['is_remux'],
        'source':           parsed['source'],
        'edition':          parsed['edition'],
        'group':            parsed['group'],
        'moviehash':        moviehash,
        'audio_tracks':     [],
        'multi_audio':      parsed['multi_audio'],
        'primary_audio':    f"{parsed['audio'] or ''} {parsed['channels'] or ''}".strip() or None,
        'browser_audio_ok': parsed['audio'] in (None, 'AAC', 'MP3', 'Opus', 'FLAC'),
        'embedded_subs':    [],
        'chapters':         [],
        'warning':          parsed['warning'],
    }
