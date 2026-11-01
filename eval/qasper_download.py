"""
One-time, human-approved download of the Qasper benchmark data (ADR 0009). The only module in `eval/` that touches
the network, and it uses the standard library only.

Why so careful: the archive comes from a third party. So: https only (also after redirects) with a verified TLS
context; a byte cap while reading; the archive's SHA-256 must equal the one pinned in the committed manifest; the
tar file is opened in memory and exactly one allow-listed regular member is read (size and member-count caps, no
links, no odd names); nothing is ever written to disk from a member name. Only the derived 50-paper subset (plain
JSON, outside the repo) is kept. First run: `python -m eval.qasper_download --record-hash` pins the hashes
(trust on first use; a human compares the size and URL, then commits the manifest).

Usage: python -m eval.qasper_download [--record-hash]
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import pathlib
import ssl
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Sequence
from typing import Any

from eval import qasper

MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_MEMBER_BYTES = 200 * 1024 * 1024
MAX_MEMBERS = 50
# Total decompressed tar size, enforced before tar parses any header (PAX headers can expand without a member size).
# 512 MiB: far above the dev archive's data file (member cap is 200 MiB) plus small siblings, far below a memory bomb.
MAX_DECOMPRESSED = 512 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 60
CHUNK = 1 << 16

Fetch = Callable[[str, int], bytes]


class DownloadError(Exception):
    """The download or the archive failed a safety check. Nothing was written."""


def validate_url(url: str) -> None:
    """Accept only absolute https URLs (rejects file:, ftp:, data:, http: and scheme-less URLs)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise DownloadError("Only https URLs are allowed.")


class HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect to anything but https before the next request is built."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def make_opener() -> urllib.request.OpenerDirector:
    """An opener with certificate and hostname verification and the https-only redirect handler (no http/ftp/file)."""
    opener = urllib.request.OpenerDirector()
    context = ssl.create_default_context()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.UnknownHandler(),
        urllib.request.HTTPSHandler(context=context),
        HttpsOnlyRedirectHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


def read_limited(stream: Any, max_bytes: int) -> bytes:
    """Read a stream fully, failing as soon as it holds more than `max_bytes`."""
    buf = bytearray()
    while True:
        part = stream.read(min(CHUNK, max_bytes + 1 - len(buf)))
        if not part:
            return bytes(buf)
        buf += part
        if len(buf) > max_bytes:
            raise DownloadError("The download is larger than the allowed size.")


def fetch_https(url: str, max_bytes: int) -> bytes:
    """Download `url` (https only) into memory, at most `max_bytes`."""
    validate_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "trustrag-eval-download"})  # noqa: S310 - https checked
    try:
        with make_opener().open(request, timeout=TIMEOUT_SECONDS) as response:
            validate_url(response.geturl())
            length = response.headers.get("Content-Length")
            if length is not None and length.isdigit() and int(length) > max_bytes:
                raise DownloadError("The download is larger than the allowed size.")
            return read_limited(response, max_bytes)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DownloadError(f"The download failed ({type(exc).__name__}).") from exc


def verify_sha256(data: bytes, expected: str | None) -> None:
    if expected is None or hashlib.sha256(data).hexdigest() != expected:
        raise DownloadError("The archive does not match the SHA-256 pinned in the manifest.")


def _safe_name(name: str) -> bool:
    return bool(name) and not name.startswith("/") and "\\" not in name and ".." not in name.split("/")


def extract_member(archive: bytes, member: str, *, max_member_bytes: int, max_members: int) -> bytes:
    """Return the bytes of the single allow-listed regular file `member`; reject anything suspicious in the archive."""
    try:
        gzipped = archive[:2] == b"\x1f\x8b"
        raw = read_limited(
            io.BytesIO(archive) if not gzipped else gzip.GzipFile(fileobj=io.BytesIO(archive)), MAX_DECOMPRESSED
        )
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            matches: list[tarfile.TarInfo] = []
            for count, info in enumerate(tar, start=1):
                if count > max_members:
                    raise DownloadError("The archive has too many members.")
                if not _safe_name(info.name):
                    raise DownloadError("The archive contains an unsafe member name.")
                if info.name == member:
                    matches.append(info)
            if len(matches) != 1:
                raise DownloadError("The archive does not contain exactly one expected data file.")
            info = matches[0]
            if not info.isreg() or info.size > max_member_bytes:
                raise DownloadError("The data file in the archive is not a regular file of allowed size.")
            stream = tar.extractfile(info)
            if stream is None:
                raise DownloadError("The data file in the archive could not be read.")
            return read_limited(stream, max_member_bytes)
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        raise DownloadError(f"The archive could not be read ({type(exc).__name__}).") from exc


def _write_atomic(path: pathlib.Path, payload: bytes, mode: int = 0o600) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def _canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")


def _check_data_dir(data_dir: pathlib.Path) -> None:
    """Validate without creating: outside the repo, not a symlink, ours, and holding at most subset.json."""
    qasper.check_outside_repo(data_dir)
    if data_dir.is_symlink():
        raise DownloadError("The data directory must not be a symlink.")
    if data_dir.exists():
        try:
            foreign = data_dir.stat().st_uid not in {os.getuid(), os.geteuid()}
            extra = any(entry.name != qasper.SUBSET_NAME for entry in data_dir.iterdir())
        except OSError as exc:
            raise DownloadError("The data directory could not be inspected.") from exc
        if foreign or extra:
            raise DownloadError("The data directory is not owned by you or holds unexpected files; use an empty one.")


def _prepare_data_dir(data_dir: pathlib.Path) -> None:
    """Create the data directory (0700), or accept an existing one only if it is ours and holds at most subset.json."""
    _check_data_dir(data_dir)
    qasper.ensure_private_dir(data_dir)


def _run(args: argparse.Namespace, fetch: Fetch, manifest_path: pathlib.Path, data_dir: pathlib.Path) -> None:
    manifest = qasper.load_manifest(manifest_path)
    pinned = manifest.get("archive_sha256")
    if args.record_hash and pinned is not None:
        raise DownloadError(
            "--record-hash refused: the manifest already pins a hash. Re-pinning is a reviewed hand edit."
        )
    if not args.record_hash and pinned is None:
        raise DownloadError("The manifest has no pinned hash yet. The first approved run needs --record-hash.")
    validate_url(manifest["source_url"])
    _prepare_data_dir(data_dir)

    archive = fetch(manifest["source_url"], MAX_ARCHIVE_BYTES)
    digest = hashlib.sha256(archive).hexdigest()
    if not args.record_hash:
        verify_sha256(archive, pinned)
    member = extract_member(
        archive, manifest["archive_member"], max_member_bytes=MAX_MEMBER_BYTES, max_members=MAX_MEMBERS
    )
    papers = qasper.parse_papers(member)
    ids = qasper.select_paper_ids(papers)
    if not ids:
        raise DownloadError("No usable papers were found in the data file.")
    subset = _canonical({paper_id: papers[paper_id] for paper_id in ids})
    subset_digest = hashlib.sha256(subset).hexdigest()
    if not args.record_hash and manifest.get("subset_sha256") not in (None, subset_digest):
        raise DownloadError("The derived subset does not match the SHA-256 pinned in the manifest.")

    _check_data_dir(data_dir)  # re-check: the directory may have been swapped while the archive downloaded
    _write_atomic(data_dir / qasper.SUBSET_NAME, subset)
    print(f"archive: {len(archive)} bytes, sha256 {digest}")
    print(f"subset: {len(ids)} papers, sha256 {subset_digest}")
    if args.record_hash:
        manifest.update(archive_sha256=digest, subset_sha256=subset_digest, paper_ids=ids)
        _write_atomic(manifest_path, (json.dumps(manifest, indent=2, ensure_ascii=True) + "\n").encode("ascii"), 0o644)
        print("manifest updated: check the values, then commit it.")


def main(
    argv: Sequence[str] | None = None,
    *,
    fetch: Fetch | None = None,
    manifest_path: pathlib.Path | None = None,
    data_dir: pathlib.Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Download and verify the Qasper benchmark subset (network).")
    parser.add_argument("--record-hash", action="store_true", help="first approved run: pin the hashes in the manifest")
    args = parser.parse_args(argv)
    try:
        _run(
            args,
            fetch or fetch_https,
            manifest_path or qasper.MANIFEST_PATH,
            data_dir if data_dir is not None else qasper.resolve_data_dir(),
        )
    except (DownloadError, qasper.QasperDataError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
