#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mirror jr-comfortable's comfort.json onto our own domain (jr-delay).

The site consumes comfort.json straight from GitHub Pages / jsDelivr today;
those CDNs have been seen hours behind. This script copies the newest valid
payload to data/comfort.json so prepare_delay_assets.py ships it as
https://jr-delay.suki.ing/comfort.json — the site's first source.

Best-effort by design: if every upstream fails (or the payload is truncated),
this run simply does not produce the file; the Worker keeps serving the
previous deployment and the site's GitHub fallbacks still work.
"""
import json
import os
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from baker.common import http_get  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, 'data', 'comfort.json')

SOURCES = [
    'https://ooep.github.io/jr-comfortable/data/comfort.json',
    'https://cdn.jsdelivr.net/gh/ooep/jr-comfortable@main/data/comfort.json',
    'https://raw.githubusercontent.com/ooep/jr-comfortable/main/data/comfort.json',
]


def valid(d):
    if not isinstance(d, dict) or d.get('version') != 1:
        return False
    trains = d.get('trains')
    if not isinstance(trains, list) or not trains or len(trains) > 8000:
        return False
    if not isinstance(d.get('by_suki_key'), dict):
        return False
    return bool(d.get('generated_at') or d.get('date'))


def main():
    for url in SOURCES:
        try:
            d = json.loads(http_get(url, retries=2, timeout=30).decode('utf-8', 'replace'))
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            print(f'[comfort] {url.rsplit("/", 1)[0]}: {type(exc).__name__}: {str(exc)[:100]}')
            continue
        if not valid(d):
            print(f'[comfort] rejected invalid payload from {url}')
            continue
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT), prefix='.comfort-', suffix='.json')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(d, f, ensure_ascii=False, separators=(',', ':'))
            os.chmod(tmp, 0o644)
            os.replace(tmp, OUT)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        print(f'[comfort] mirrored gen={d.get("generated_at")} trains={len(d["trains"])} '
              f'bytes={os.path.getsize(OUT)}')
        return 0
    print('[comfort] all sources failed; nothing written this run')
    return 0


if __name__ == '__main__':
    sys.exit(main())
