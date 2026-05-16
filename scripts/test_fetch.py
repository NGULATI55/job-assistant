"""Quick CLI helper to test the SEEK fetcher against a real URL.

Usage:
    python scripts\\test_fetch.py "https://www.seek.com.au/job/12345678"

Prints the normalised job dict and lists any missing fields. Useful for debugging
without launching Streamlit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Force UTF-8 stdout so emoji etc. in real job ads don't crash the print on Windows cp1252.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from core import seek_fetch  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    url = argv[1]
    print(f"Fetching: {url}\n")
    try:
        job, missing = seek_fetch.fetch_from_url(url)
    except seek_fetch.FetchError as e:
        print(f"FetchError: {e}")
        return 1

    print(json.dumps(job, indent=2, ensure_ascii=False))
    print()
    if missing:
        print(f"Partial extraction. Missing required fields: {', '.join(missing)}")
    else:
        print("All required fields present.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
