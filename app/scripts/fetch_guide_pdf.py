"""
Download one product guide PDF from Drive with the service account, read-only.

    python -m app.scripts.fetch_guide_pdf FILE_ID --out-dir DIR

The key file's path comes from GOOGLE_SERVICE_ACCOUNT_KEY_FILE. Prints the saved
path as the last line, and the file's name, size and time before it. A download that
does not match Drive's own size and MD5 is deleted and fails.
"""
import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

_API = "https://www.googleapis.com/drive/v3/files"
_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_CHUNK = 1 << 20  # 258 MB goes to disk a chunk at a time, not into memory


def download(session, file_id: str, out_dir: Path) -> Path:
    """*session* is an authorised requests-style session."""
    url = f"{_API}/{file_id}"
    response = session.get(url, params={"fields": "name,mimeType,size,md5Checksum", "supportsAllDrives": "true"})
    response.raise_for_status()
    meta = response.json()
    if meta["mimeType"] != "application/pdf":
        raise ValueError(f"{meta['name']} is {meta['mimeType']}, not a PDF")

    target = out_dir / Path(meta["name"]).name  # the name is Drive's, not a path of ours
    digest, size = hashlib.md5(), 0
    with session.get(url, params={"alt": "media", "supportsAllDrives": "true"}, stream=True) as body:
        body.raise_for_status()
        with target.open("wb") as handle:
            for chunk in body.iter_content(_CHUNK):
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)

    if size != int(meta["size"]) or digest.hexdigest() != meta["md5Checksum"]:
        target.unlink()
        raise ValueError(f"{meta['name']}: download does not match Drive's size and MD5")
    return target


def main() -> int:
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file_id")
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    credentials = service_account.Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_KEY_FILE"], scopes=_SCOPES
    )
    started = time.monotonic()
    path = download(AuthorizedSession(credentials), args.file_id, args.out_dir)
    print(f"{path.name}: {path.stat().st_size / 1e6:.1f} MB in {time.monotonic() - started:.1f} s", file=sys.stderr)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
