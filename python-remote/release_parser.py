"""Parse movie and UHD Blu-ray remux release names into structured metadata.

Handles scene, P2P, and remux naming conventions such as:
  Dune.Part.Two.2024.2160p.UHD.BluRay.Remux.HEVC.DV.HDR10.TrueHD.7.1.Atmos-FraMeSToR.mkv
  Oppenheimer (2023) [2160p BluRay REMUX x265 10bit HDR10+ DTS-HD MA 5.1].mkv
  Blade.Runner.2049.2017.FINAL.CUT.2160p.WEB-DL.DDP5.1.Atmos.DV.MP4-FLUX.mp4
  The.Shawshank.Redemption.1994.REMASTERED.1080p.BluRay.x264-SPARKS.mkv
"""

import re

VIDEO_EXTS = ('.mkv', '.mp4', '.m4v', '.mov', '.webm', '.avi', '.m2ts', '.ts')

_RESOLUTION = [
    (r'\b2160p?\b|\b4k\b|\buhd\b', '2160p'),
    (r'\b1440p\b',                 '1440p'),
    (r'\b1080p?\b|\bfhd\b',        '1080p'),
    (r'\b720p?\b',                 '720p'),
    (r'\b576p\b',                  '576p'),
    (r'\b480p?\b',                 '480p'),
]

_CODEC = [
    (r'\bav1\b',                                    'AV1'),
    (r'\bhevc\b|\bh\.?\s?265\b|\bx\.?265\b',        'HEVC'),
    (r'\bavc\b|\bh\.?\s?264\b|\bx\.?264\b',         'H264'),
    (r'\bvp9\b',                                    'VP9'),
    (r'\bvc-?1\b',                                  'VC-1'),
    (r'\bmpeg-?2\b',                                'MPEG-2'),
]

_SOURCE = [
    (r'\b(?:uhd[\s._\-]+)?(?:blu-?ray|bd)[\s._\-]+remux\b|\bremux\b', 'BluRay Remux'),
    (r'\buhd[\s._\-]+blu-?ray\b',                                     'UHD BluRay'),
    (r'\bblu-?ray\b|\bbd-?rip\b|\bbr-?rip\b',                         'BluRay'),
    (r'\bweb-?dl\b',                                                  'WEB-DL'),
    (r'\bweb-?rip\b|\bweb\b',                                         'WEBRip'),
    (r'\bhdtv\b',                                                     'HDTV'),
    (r'\bdvd-?rip\b|\bdvd\b',                                         'DVD'),
]

_AUDIO = [
    (r'\btruehd[\s._\-]*atmos\b|\batmos[\s._\-]*truehd\b', 'TrueHD Atmos'),
    (r'\btruehd\b',                                        'TrueHD'),
    (r'\bdts[\s._\-]*hd[\s._\-]*ma\b|\bdts-?ma\b',         'DTS-HD MA'),
    (r'\bdts[\s._\-]*hd[\s._\-]*hra\b',                    'DTS-HD HRA'),
    (r'\bdts[\s._\-]*x\b',                                 'DTS:X'),
    (r'\bdts\b',                                           'DTS'),
    (r'\b(?:e-?ac-?3|ddp|dd\+)[\s._\-]*(?:\d\.\d[\s._\-]*)?atmos\b', 'E-AC-3 Atmos'),
    (r'\be-?ac-?3\b|\bddp(?:\s?\d\.\d)?\b|\bdd\+\b',       'E-AC-3'),
    (r'\bac-?3\b|\bdd(?:\s?[257]\.[01])\b',                'AC-3'),
    (r'\blpcm\b|\bpcm\b',                                  'PCM'),
    (r'\bflac\b',                                          'FLAC'),
    (r'\bopus\b',                                          'Opus'),
    (r'\baac\b',                                           'AAC'),
    (r'\bmp3\b',                                           'MP3'),
]

_CHANNELS = [
    (r'\b7\.1\b', '7.1'),
    (r'\b5\.1\b', '5.1'),
    (r'\b2\.1\b', '2.1'),
    (r'\b2\.0\b', '2.0'),
    (r'\b1\.0\b', '1.0'),
]

_EDITION = [
    (r'\bdirector\'?s[\s._\-]+cut\b', 'Director\'s Cut'),
    (r'\bextended(?:[\s._\-]+(?:cut|edition))?\b', 'Extended'),
    (r'\bfinal[\s._\-]+cut\b',        'Final Cut'),
    (r'\btheatrical(?:[\s._\-]+cut)?\b', 'Theatrical'),
    (r'\bimax(?:[\s._\-]+edition)?\b', 'IMAX'),
    (r'\bremastered\b',               'Remastered'),
    (r'\bunrated\b',                  'Unrated'),
    (r'\bcriterion\b',                'Criterion'),
]


def _find(text, table):
    for pattern, canonical in table:
        if re.search(pattern, text, re.I):
            return canonical
    return None


def _detect_hdr(name):
    """Return canonical HDR format label ('HDR10+', 'Dolby Vision', 'HDR10', 'HLG', or None)."""
    has_dv = bool(re.search(r'\bdolby[\s._\-]?vision\b|\bdovi\b|\bdv\b', name, re.I))
    has_hdr10plus = bool(re.search(r'\bhdr10\+|hdr10plus\b', name, re.I))
    has_hdr10 = bool(re.search(r'\bhdr10\b|\bhdr\b', name, re.I))
    has_hlg = bool(re.search(r'\bhlg\b', name, re.I))

    if has_dv and (has_hdr10plus or has_hdr10):
        return 'DV / HDR10'
    if has_hdr10plus:
        return 'HDR10+'
    if has_dv:
        return 'Dolby Vision'
    if has_hdr10:
        return 'HDR10'
    if has_hlg:
        return 'HLG'
    return None


# Tokens that start technical release tags after the movie title/year
_CUTOFF_TOKENS = re.compile(
    r'\b(?:'
    r'2160p|1440p|1080p|720p|576p|480p|4k|uhd|fhd|'
    r'remux|blu-?ray|bdrip|brrip|web-?dl|webrip|hdtv|dvdrip|'
    r'hevc|x265|h\.?265|avc|x264|h\.?264|av1|vp9|vc-?1|'
    r'hdr10\+?|dolby[\s._\-]?vision|dovi|dv|hlg|sdr|'
    r'truehd|dts(?:-?hd)?|atmos|eac3|ddp|ac3|flac|aac|'
    r'10bit|10-bit|hi10p|'
    r'director\'?s[\s._\-]+cut|extended|final[\s._\-]+cut|theatrical|imax|remastered|unrated|'
    r'proper|repack\d*|multi|dual[\s._\-]?audio'
    r')\b',
    re.I,
)


def parse(raw):
    """Parse a movie filename, folder name, or torrent release title."""
    name = (raw or '').strip()
    for ext in VIDEO_EXTS:
        if name.lower().endswith(ext):
            name = name[:-len(ext)]
            break

    # Normalize dots/underscores to spaces (preserving channel decimals like 7.1, 5.1, 2.0)
    normalized = re.sub(r'(?<![\d])\.|\.(?![01]\b)|_', ' ', name)

    resolution = _find(normalized, _RESOLUTION)
    codec      = _find(normalized, _CODEC)
    source     = _find(normalized, _SOURCE)
    audio      = _find(normalized, _AUDIO)
    channels   = _find(normalized, _CHANNELS)
    edition    = _find(normalized, _EDITION)
    hdr_type   = _detect_hdr(normalized)
    is_remux   = bool(re.search(r'\bremux\b', normalized, re.I))
    ten_bit    = bool(re.search(r'\b10[\s\-]?bits?\b|\bhi10p?\b|\bmain\s*10\b', normalized, re.I)) or bool(hdr_type)
    multi_audio = bool(re.search(r'\b(?:dual|multi)[\s._\-]?audio\b|\bmulti\b', normalized, re.I))
    has_atmos  = bool(re.search(r'\batmos\b', normalized, re.I))
    has_dv     = bool(re.search(r'\bdolby[\s._\-]?vision\b|\bdovi\b|\bdv\b', normalized, re.I))

    # Extract release group at the end (e.g. "-FraMeSToR", "-SPARKS", "[YTS.MX]")
    group = None
    m_grp = re.search(r'(?:-|\b)([A-Za-z0-9]+)\s*(?:\[[^\]]+\])?\s*$', name)
    if m_grp and len(m_grp.group(1)) >= 2 and not re.fullmatch(r'\d+p?|[257]\.[01]|mkv|mp4', m_grp.group(1), re.I):
        group = m_grp.group(1)

    # Find movie year: look for (19xx|20xx) or bare 19xx/20xx.
    # For titles like "2001 A Space Odyssey 1968" or "Blade Runner 2049 2017",
    # the release year is the LAST valid year before technical tokens.
    cutoff_match = _CUTOFF_TOKENS.search(normalized)
    head = normalized[:cutoff_match.start()] if cutoff_match else normalized

    year = None
    title_part = head

    year_matches = list(re.finditer(r'[\(\[\s]((?:19\d{2}|20[0-2]\d))(?=[\)\]\s]|$)', ' ' + head))
    if year_matches:
        last_yr = year_matches[-1]
        # Adjust index for the prepended space
        yr_start = max(0, last_yr.start(1) - 1)
        year = int(last_yr.group(1))
        if yr_start > 0:
            title_part = head[:yr_start]

    # Strip brackets and leftover punctuation from title
    title_part = re.sub(r'[\[\(][^\]\)]*[\]\)]', ' ', title_part)
    title_part = re.sub(r'\s+', ' ', title_part).strip(' .-–_~()[]')
    if not title_part:
        # Fallback if title started with a year (e.g. "1917 (2019)")
        title_part = re.sub(r'[\[\(][^\]\)]*[\]\)]', ' ', head).strip(' .-–_~()[]') or raw

    return {
        'raw':         raw,
        'title':       title_part,
        'year':        year,
        'edition':     edition,
        'resolution':  resolution,
        'source':      source,
        'is_remux':    is_remux,
        'codec':       codec,
        'ten_bit':     ten_bit,
        'hdr':         hdr_type is not None,
        'hdr_type':    hdr_type,
        'dolby_vision': has_dv,
        'audio':       audio,
        'channels':    channels,
        'atmos':       has_atmos,
        'multi_audio': multi_audio,
        'group':       group,
        'warning':     playback_warning(resolution, codec, hdr_type=hdr_type, is_remux=is_remux),
    }


def playback_warning(resolution, codec, hdr_type=None, is_remux=False):
    """Return a Pi 5 hardware playback warning if applicable, else None."""
    if codec == 'AV1':
        if resolution == '2160p':
            return 'AV1 has no hardware decoder on the Pi 5 — 4K AV1 will not play smoothly'
        return 'AV1 is software-decoded on the Pi 5 and may stutter at high bitrates'
    if resolution == '2160p' and codec == 'H264':
        return '4K H.264 has no hardware decoder on the Pi 5 and will stutter'
    if resolution == '2160p' and codec in ('VC-1', 'MPEG-2'):
        return f'4K {codec} is software-decoded on the Pi 5'
    if hdr_type == 'Dolby Vision' and not is_remux:
        # Profile 5 DV (streaming WEB-DL without HDR10 fallback layer) uses
        # proprietary ICtCp reshaping; mpv/libplacebo tone-maps it if supported,
        # whereas Profile 7/8 Blu-ray Remuxes carry a standard HDR10 base layer.
        return None
    return None


def parse_release_name(raw):
    """Return parsed release dictionary with normalized keys for library & acquisition."""
    res = parse(raw)
    warn = res.get('warning')
    return {
        **res,
        'clean_title':    res.get('title') or raw,
        'is_4k':          res.get('resolution') == '2160p',
        'hdr':            res.get('hdr_type') or 'SDR',
        'video_codec':    res.get('codec'),
        'audio_codec':    res.get('audio'),
        'warnings':       [warn] if warn else [],
        'pi5_compatible': warn is None,
    }


if __name__ == '__main__':
    SAMPLES = [
        'Dune.Part.Two.2024.2160p.UHD.BluRay.Remux.HEVC.DV.HDR10.TrueHD.7.1.Atmos-FraMeSToR.mkv',
        'Blade.Runner.2049.2017.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.TrueHD.7.1.Atmos-FGT.mkv',
        'Oppenheimer (2023) [2160p BluRay x265 10bit HDR10+ DTS-HD MA 5.1].mkv',
        '1917.2019.2160p.UHD.BluRay.x265.10bit.HDR.TrueHD.7.1.Atmos-SWTYBLZ.mkv',
        'The.Lord.of.the.Rings.The.Fellowship.of.the.Ring.2001.EXTENDED.2160p.UHD.BluRay.Remux.HEVC.HDR10.TrueHD.7.1-PmP.mkv',
        'Anatomy.of.a.Fall.2023.1080p.BluRay.x264.DTS-HD.MA.5.1-MT.mkv',
        'Civil.War.2024.2160p.WEB-DL.AV1.DDP5.1.mkv',
    ]
    for sample in SAMPLES:
        p = parse(sample)
        print(
            f"{p['title']} ({p['year']}) | {p['resolution']} | {p['source']} | "
            f"{p['codec']} | HDR={p['hdr_type']} | Audio={p['audio']} {p['channels']} | "
            f"Remux={p['is_remux']} | Warn={p['warning']}"
        )
