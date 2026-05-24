"""Download the public Polymarket trade dataset.

Source: https://github.com/warproxxx/poly_data — every trade ever made on
Polymarket (~86M rows). We pull the published S3 snapshot rather than rebuilding
from the Goldsky subgraph, then decompress it locally.

Files land in ``data/`` (gitignored). Re-running skips work that's already done.
"""

from __future__ import annotations

import argparse
import lzma
import shutil
from pathlib import Path

import httpx

SNAPSHOT_URL = "https://polydata-archive.s3.us-east-1.amazonaws.com/orderFilled_complete.csv.xz"
ARCHIVE_URL = "https://polydata-archive.s3.us-east-1.amazonaws.com/archive.tar.xz"

DATA_DIR = Path(__file__).resolve().parents[3] / "data"


def _stream_download(url: str, dest: Path, *, chunk: int = 1 << 20) -> None:
    """Stream ``url`` to ``dest`` with a running progress readout."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=None) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with tmp.open("wb") as f:
            for block in r.iter_bytes(chunk):
                f.write(block)
                done += len(block)
                if total:
                    pct = done / total * 100
                    print(f"\r  {done / 1e9:.2f} / {total / 1e9:.2f} GB ({pct:5.1f}%)", end="")
                else:
                    print(f"\r  {done / 1e9:.2f} GB", end="")
    print()
    tmp.rename(dest)


def _decompress_xz(src: Path, dest: Path, *, chunk: int = 1 << 20) -> None:
    """Decompress an ``.xz`` file by streaming (the CSV is far too big for RAM)."""
    with lzma.open(src, "rb") as fin, dest.open("wb") as fout:
        shutil.copyfileobj(fin, fout, length=chunk)


def download_snapshot(data_dir: Path = DATA_DIR, *, force: bool = False) -> Path:
    """Download and decompress the order-fill snapshot. Returns the CSV path."""
    data_dir.mkdir(parents=True, exist_ok=True)
    xz_path = data_dir / "orderFilled_complete.csv.xz"
    csv_path = data_dir / "orderFilled_complete.csv"

    if csv_path.exists() and not force:
        print(f"Already have {csv_path} ({csv_path.stat().st_size / 1e9:.1f} GB) — skipping.")
        return csv_path

    if not xz_path.exists() or force:
        print(f"Downloading {SNAPSHOT_URL}")
        _stream_download(SNAPSHOT_URL, xz_path)

    print(f"Decompressing {xz_path.name} -> {csv_path.name}")
    _decompress_xz(xz_path, csv_path)
    print(f"Done. CSV at {csv_path} ({csv_path.stat().st_size / 1e9:.1f} GB).")
    return csv_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Download the Polymarket trade dataset.")
    ap.add_argument("--force", action="store_true", help="re-download even if files exist")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR, help="where to store data")
    args = ap.parse_args()
    download_snapshot(args.data_dir, force=args.force)


if __name__ == "__main__":
    main()
