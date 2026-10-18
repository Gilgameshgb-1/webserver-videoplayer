"""Backwards-compatible facade over `acquisition.py`.

Preserves the legacy `torrent_downloader` function signatures (`search_yts`,
`start_download`, `get_download_status`, `cancel_download`) while delegating
to the decoupled `acquisition` subsystem.
"""

from typing import Any, Dict, List, Optional

import acquisition
import config

MOVIES_DIR = config.MOVIES_DIR
TRACKERS = acquisition.TRACKERS


def search_yts(query: str) -> List[Dict[str, Any]]:
    return acquisition.search_yts(query)


def start_download(info_hash: str, title: str, save_root: Optional[str] = None) -> bool:
    ok, _, _ = acquisition.start_download(info_hash, title, save_root=save_root)
    return ok


def get_download_status() -> Dict[str, Dict[str, Any]]:
    return acquisition.get_download_status()


def cancel_download(info_hash: str) -> bool:
    return acquisition.cancel_download(info_hash, delete_files=True)
