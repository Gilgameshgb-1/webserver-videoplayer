"""Drive mpv / wserVideoPlayer over its JSON IPC socket.

Reuses the proven request_id-framed IPC protocol from anime.pi (`anime-stream/player.py`)
and extends it for 4K UHD Blu-ray remuxes:
  1. Newline-delimited, request_id-matched socket framing (64 KB chunks) so
     large Blu-ray `track-list` replies (often 30+ tracks) never truncate or
     collide with asynchronous mpv events.
  2. Automatic registration of external sidecar subtitles (`sub-add`) with
     language and provenance labels.
  3. Live subtitle delay (`sub-delay`) control for synchronizing external
     Croatian/English subtitles without re-muxing.
  4. Runtime audio downmix / HDMI passthrough switching (`stereo`, `auto-safe`,
     `passthrough`) for TrueHD 7.1 Atmos and DTS-HD MA tracks.
  5. Live hardware decode (`hwdec-current`), HDR, and frame-drop telemetry.
"""

import json
import os
import platform
import socket
import subprocess
import threading
import time

import config
import media_probe

_counter = [0]
_lock = threading.Lock()
_process = [None]
_audio_mode = ['auto-safe']


def _next_id():
    with _lock:
        _counter[0] += 1
        return _counter[0]


def command(args, timeout=1.8):
    """Send one mpv IPC command and wait for its matching request_id reply."""
    request_id = _next_id()
    payload = json.dumps({'command': args, 'request_id': request_id}) + '\n'

    if platform.system() == 'Windows':
        try:
            import win32file
            import win32pipe
            handle = win32file.CreateFile(
                config.MPV_IPC_PATH,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            )
            win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
            win32file.WriteFile(handle, payload.encode('utf-8'))
            _, data = win32file.ReadFile(handle, 65536)
            win32file.CloseHandle(handle)
            for line in data.decode('utf-8', 'replace').splitlines():
                if not line.strip():
                    continue
                msg = json.loads(line)
                if msg.get('request_id') == request_id or 'request_id' not in msg:
                    return msg
            return None
        except Exception:
            return None

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(config.MPV_IPC_PATH)
            sock.sendall(payload.encode('utf-8'))

            buffer = b''
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buffer += chunk

                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line.decode('utf-8', 'replace'))
                    except ValueError:
                        continue
                    if message.get('request_id') == request_id:
                        return message
            return None
    except (OSError, socket.error):
        return None


def get_property(name, default=None):
    reply = command(['get_property', name])
    if isinstance(reply, dict) and reply.get('error') == 'success':
        return reply.get('data', default)
    return default


def set_property(name, value):
    return command(['set_property', name, value])


def show_osd(text, duration_ms=2000):
    return command(['show-text', str(text), int(duration_ms)])


def is_running():
    return command(['get_property', 'idle-active'], timeout=0.6) is not None


def ensure_running(wait_seconds=6.0):
    """Launch wserVideoPlayer / mpv if its IPC socket is not responding."""
    if is_running():
        return True

    proc = _process[0]
    if proc is None or proc.poll() is not None:
        try:
            env = os.environ.copy()
            env['MPV_IPC_PATH'] = config.MPV_IPC_PATH
            _process[0] = subprocess.Popen(
                [config.MPV_EXE] + list(config.MPV_ARGS),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except (OSError, FileNotFoundError):
            return False

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(0.2)
        if is_running():
            return True
    return False


def load(abs_path, start_seconds=0, sidecar_subs=None, sub_delay=0.0):
    """Load `abs_path` on the TV player, attach sidecar subs, and apply sub_delay."""
    if not ensure_running():
        return False

    set_property('start', str(int(start_seconds or 0)))
    reply = command(['loadfile', abs_path, 'replace'], timeout=3.0)
    if reply is None:
        return False

    # Wait briefly for mpv to demux the container headers and autoload sidecars
    # before attaching extra sidecar subtitles or setting sub-delay.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        loaded_path = get_property('path')
        idle = get_property('idle-active')
        tlist = get_property('track-list')
        if loaded_path and not idle and tlist:
            break
        time.sleep(0.1)

    if sub_delay:
        set_sub_delay(sub_delay)
    else:
        set_sub_delay(0.0)

    if sidecar_subs:
        existing_raw = get_property('track-list') or []
        existing_files = set()
        for t in existing_raw:
            if t.get('type') == 'sub' and t.get('external'):
                ef = t.get('external-filename') or t.get('title') or ''
                if ef:
                    existing_files.add(os.path.abspath(ef))
                    existing_files.add(os.path.basename(ef))

        preferred_hr = False
        for sub in sidecar_subs:
            sub_abs = sub.get('abs_path')
            if not sub_abs or not os.path.isfile(sub_abs):
                continue
            lang = sub.get('lang_code') or ''
            if lang == 'hr':
                preferred_hr = True
            if os.path.abspath(sub_abs) in existing_files or os.path.basename(sub_abs) in existing_files:
                continue
            title = sub.get('lang_name') or sub.get('label') or 'External'
            add_subtitle_file(sub_abs, flags='auto', title=title, lang=lang)

        # If a Croatian subtitle (embedded or sidecar) exists, select it automatically
        # when no Croatian track is currently active.
        current_tracks = tracks().get('sub', [])
        if any(t.get('lang_code') == 'hr' for t in current_tracks):
            selected_hr = any(t.get('selected') and t.get('lang_code') == 'hr' for t in current_tracks)
            if not selected_hr and preferred_hr:
                for tr in current_tracks:
                    if tr.get('lang_code') == 'hr':
                        set_track('sub', tr['id'])
                        break

    return True


def stop():
    return command(['stop'])


def quit_player():
    command(['quit'])
    proc = _process[0]
    if proc is not None:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        _process[0] = None
    return True


def seek(value, mode='relative'):
    return command(['seek', str(value), mode])


def cycle_pause():
    return command(['cycle', 'pause'])


def status():
    """Return current playback state for the 1s remote heartbeat."""
    pos = get_property('time-pos')
    if pos is None:
        return None
    duration = get_property('duration') or 0
    percent = get_property('percent-pos') or 0
    sub_delay = get_property('sub-delay') or 0.0
    hwdec = get_property('hwdec-current') or 'no'
    dropped = get_property('frame-drop-count') or 0

    total = max(0, int(pos))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)

    return {
        'position':       round(float(pos), 2),
        'duration':       round(float(duration), 2),
        'percent':        round(float(percent), 2),
        'paused':         bool(get_property('pause')),
        'time':           f'{hours}:{minutes:02d}:{seconds:02d}',
        'path':           get_property('path') or '',
        'sub_delay':      round(float(sub_delay), 2),
        'hwdec':          hwdec,
        'dropped_frames': int(dropped),
        'audio_mode':     _audio_mode[0],
    }


def tracks():
    """Return runtime audio and subtitle tracks from mpv's `track-list`."""
    raw = get_property('track-list') or []
    out = {'audio': [], 'sub': []}
    for track in raw:
        kind = track.get('type')
        if kind not in ('audio', 'sub'):
            continue
        raw_lang = track.get('lang') or ''
        title = track.get('title') or ''
        external = bool(track.get('external'))
        ext_file = track.get('external-filename') or ''

        # For external sidecars autoloaded by mpv, title is the raw filename
        clean_title = title
        if external and (title.lower().endswith(config.SUB_EXTS) or os.sep in title):
            clean_title = ''

        lang_code, lang_name = media_probe.normalize_language(raw_lang, clean_title or os.path.basename(ext_file))
        codec = (track.get('codec') or '').lower()
        forced = bool(track.get('forced'))
        default = bool(track.get('default'))
        selected = bool(track.get('selected'))
        channels = track.get('demux-channel-count') or 0
        is_bitmap = codec in media_probe.BITMAP_SUB_CODECS

        bits = []
        if lang_code != 'und':
            bits.append(lang_name)
        elif raw_lang:
            bits.append(raw_lang.upper())
        if clean_title and not clean_title.lower().startswith(lang_name.lower()) and clean_title.lower() != raw_lang.lower():
            bits.append(clean_title)
        if kind == 'audio' and codec:
            a_lbl = media_probe._audio_codec_label(codec, title=clean_title)
            if a_lbl.lower() not in ' '.join(bits).lower():
                bits.append(a_lbl)
            if channels:
                ch_lbl = media_probe._channels_label(channels)
                if ch_lbl not in ' '.join(bits):
                    bits.append(ch_lbl)
        if kind == 'sub':
            if forced and 'forced' not in clean_title.lower():
                bits.append('Forced')
            if is_bitmap:
                bits.append('PGS')
            if external and 'sidecar' not in clean_title.lower() and 'external' not in clean_title.lower():
                bits.append('Sidecar')

        label = ' · '.join(dict.fromkeys(b for b in bits if b)) or f"Track {track.get('id')}"
        out[kind].append({
            'id':        track.get('id'),
            'label':     label,
            'lang':      raw_lang,
            'lang_code': lang_code,
            'lang_name': lang_name,
            'title':     clean_title,
            'codec':     codec,
            'channels':  channels,
            'default':   default,
            'forced':    forced,
            'external':  external,
            'is_bitmap': is_bitmap,
            'selected':  selected,
        })
    return out


def set_track(kind, track_id):
    """Select an audio (`aid`) or subtitle (`sid`) track (`'no'` disables)."""
    prop = {'audio': 'aid', 'sub': 'sid'}.get(kind)
    if not prop:
        return None
    if track_id in (0, '0', 'no', 'off', False):
        track_id = 'no'
    return set_property(prop, track_id)


def add_subtitle_file(abs_path, flags='select', title=None, lang=None, select=None):
    """Attach a sidecar subtitle file to the running player."""
    if not os.path.isfile(abs_path):
        return None
    if select is True:
        flags = 'select'
    elif select is False:
        flags = 'auto'
    cmd = ['sub-add', abs_path, flags]
    if title is not None:
        cmd.append(str(title))
        if lang is not None:
            cmd.append(str(lang))
    return command(cmd)


def get_sub_delay():
    val = get_property('sub-delay', 0.0)
    try:
        return round(float(val or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def set_sub_delay(seconds):
    val = round(float(seconds or 0.0), 2)
    set_property('sub-delay', val)
    sign = '+' if val > 0 else ''
    show_osd(f'Subtitle delay: {sign}{val:.1f}s')
    return val


def adjust_sub_delay(delta_seconds):
    current = get_sub_delay()
    return set_sub_delay(current + float(delta_seconds or 0.0))


def set_audio_mode(mode):
    """Switch audio output mode for Blu-ray remuxes:
      * 'stereo'      - force 2.0 downmix with center-channel boost
      * 'auto-safe'   - standard HDMI channel negotiation
      * 'passthrough' - bitstream TrueHD / DTS-HD / E-AC-3 / AC-3 over HDMI
    """
    if mode not in ('stereo', 'auto-safe', 'passthrough'):
        return None
    _audio_mode[0] = mode
    if mode == 'stereo':
        set_property('audio-spdif', '')
        set_property('audio-channels', 'stereo')
        set_property('ad-lavc-downmix', 'yes')
        show_osd('Audio: Stereo Downmix (2.0)')
    elif mode == 'passthrough':
        set_property('audio-channels', 'auto')
        set_property('audio-spdif', 'ac3,eac3,dts-hd,truehd')
        show_osd('Audio: HDMI Bitstream Passthrough')
    else:
        set_property('audio-spdif', '')
        set_property('audio-channels', 'auto-safe')
        set_property('ad-lavc-downmix', 'yes')
        show_osd('Audio: Auto HDMI')
    return _audio_mode[0]


def chapters():
    """Return chapter list from the currently loaded file."""
    raw = get_property('chapter-list') or []
    cur = get_property('chapter')
    out = []
    for idx, ch in enumerate(raw):
        out.append({
            'index':   idx,
            'title':   ch.get('title') or f'Chapter {idx + 1}',
            'time':    round(float(ch.get('time') or 0), 2),
            'current': idx == cur,
        })
    return out


def set_chapter(index):
    return set_property('chapter', int(index))


def diagnostics():
    """Return live 4K / HDR / hardware decode telemetry from mpv."""
    if not is_running():
        return {'running': False}
    vparams = get_property('video-params') or {}
    return {
        'running':         True,
        'hwdec':           get_property('hwdec-current') or 'no',
        'video_codec':     get_property('video-codec') or '',
        'audio_codec':     get_property('audio-codec-name') or '',
        'width':           vparams.get('w') or 0,
        'height':          vparams.get('h') or 0,
        'pixelformat':     vparams.get('pixelformat') or '',
        'hw_subformat':    vparams.get('hw-subformat') or '',
        'colormatrix':     vparams.get('colormatrix') or '',
        'primaries':       vparams.get('primaries') or '',
        'gamma':           vparams.get('gamma') or '',
        'peak_nits':       vparams.get('sig-peak') or 0,
        'dropped_frames':  get_property('frame-drop-count') or 0,
        'vo_delayed':      get_property('vo-delayed-frame-count') or 0,
        'cache_seconds':   round(float(get_property('demuxer-cache-duration') or 0), 1),
        'video_bitrate':   int(get_property('video-bitrate') or 0),
        'audio_bitrate':   int(get_property('audio-bitrate') or 0),
        'audio_mode':      _audio_mode[0],
        'sub_delay':       get_sub_delay(),
    }


def toggle_pause():
    return cycle_pause()


def seek_absolute(pos):
    return seek(pos, mode='absolute') is not None


def seek_relative(delta):
    return seek(delta, mode='relative') is not None


def set_subtitle_track(tid):
    return set_track('sub', tid) is not None


def set_audio_track(aid):
    return set_track('audio', aid) is not None


def set_audio_output_mode(mode):
    res = set_audio_mode(mode)
    return {'status': 'ok' if res else 'invalid', 'mode': res or _audio_mode[0]}


def set_volume(vol):
    return set_property('volume', float(vol))


def get_status():
    """Return comprehensive player status dictionary for remote.py & app.js."""
    if not is_running():
        return {
            'running': False,
            'idle': True,
            'paused': True,
            'position': 0.0,
            'duration': 0.0,
            'filename': '',
            'media_title': '',
            'tracks': {'video': [], 'audio': [], 'sub': []},
            'aid': 1,
            'sid': 'no',
            'sub_delay': 0.0,
            'volume': 100.0,
            'hwdec_current': 'no',
            'video_params': {},
            'audio_params': {},
            'fps': 0.0,
            'dropped_frames': 0,
            'chapters': [],
        }

    idle = bool(get_property('idle-active'))
    pos = float(get_property('time-pos') or 0.0)
    dur = float(get_property('duration') or 0.0)
    paused = bool(get_property('pause'))
    path = get_property('path') or ''
    filename = os.path.basename(path) if path else (get_property('filename') or '')
    media_title = get_property('media-title') or filename
    tr = tracks()
    aid = get_property('aid')
    sid = get_property('sid')
    sub_delay = get_sub_delay()
    vol = float(get_property('volume') or 100.0)
    hwdec = get_property('hwdec-current') or 'no'
    vparams = get_property('video-params') or {}
    aparams = get_property('audio-params') or {}
    fps = float(get_property('container-fps') or get_property('estimated-vf-fps') or 0.0)
    dropped = int(get_property('frame-drop-count') or 0)
    chaps = chapters()

    return {
        'running': True,
        'idle': idle,
        'paused': paused,
        'position': round(pos, 2),
        'duration': round(dur, 2),
        'path': path,
        'filename': filename,
        'media_title': media_title,
        'tracks': {'video': [], 'audio': tr.get('audio', []), 'sub': tr.get('sub', [])},
        'aid': aid,
        'sid': sid,
        'sub_delay': sub_delay,
        'volume': round(vol, 1),
        'hwdec_current': hwdec,
        'video_params': vparams,
        'audio_params': aparams,
        'fps': round(fps, 3),
        'dropped_frames': dropped,
        'audio_mode': _audio_mode[0],
        'chapters': chaps,
    }


def play_file(video_path, start_pos=0.0, sidecar_subs=None, sid=None, aid=None, sub_delay=0.0, audio_mode='auto-safe'):
    """Start playing `video_path`, attach sidecar subtitles, and return status."""
    norm_subs = []
    for item in (sidecar_subs or []):
        if isinstance(item, str):
            norm_subs.append({'abs_path': item})
        elif isinstance(item, dict):
            norm_subs.append(item)

    ok = load(video_path, start_seconds=start_pos, sidecar_subs=norm_subs, sub_delay=sub_delay)
    if audio_mode:
        set_audio_mode(audio_mode)

    cur_tracks = tracks()
    audio_list = cur_tracks.get('audio', [])
    sub_list = cur_tracks.get('sub', [])

    if aid is not None:
        set_track('audio', aid)
    elif len(audio_list) > 1:
        # Prefer Original English (or Croatian) audio track when Track 1 is a foreign dub (e.g. Russian)
        sel_aud = next((a for a in audio_list if a.get('selected')), None)
        sel_lang = (sel_aud.get('lang_code') or '') if sel_aud else ''
        if sel_lang not in ('en', 'hr'):
            en_candidates = [
                a for a in audio_list
                if a.get('lang_code') in ('en', 'hr')
                and 'commentary' not in (a.get('title') or '').lower()
            ]
            if en_candidates:
                set_track('audio', en_candidates[0]['id'])

    if sid is not None:
        set_track('sub', sid)
    else:
        # Prefer Croatian subtitle if available; otherwise avoid leaving Russian forced subs active
        hr_sub = next((s for s in sub_list if s.get('lang_code') == 'hr'), None)
        if hr_sub:
            set_track('sub', hr_sub['id'])
        else:
            sel_sub = next((s for s in sub_list if s.get('selected')), None)
            if sel_sub and (sel_sub.get('lang_code') or '') not in ('hr', 'en'):
                en_sub = next((s for s in sub_list if s.get('lang_code') == 'en' and not s.get('hearing_impaired')), None)
                set_track('sub', en_sub['id'] if en_sub else 'no')

    return {
        'ok': ok,
        'status': get_status(),
    }

