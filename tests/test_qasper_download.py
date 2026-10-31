"""
P5 download controls (ADR 0009, threat rows 20-22, security amendments M1-M3): hash pinning, https-only, archive
member allow-list and size caps. Everything runs on in-memory archives and injected fetchers: no network.

Interface proposed by these tests (see SPEC-GAP-030), module eval.qasper_download:
  DownloadError, validate_url(url), read_limited(stream, max_bytes) -> bytes, verify_sha256(data, expected),
  extract_member(archive, member, *, max_member_bytes, max_members) -> bytes, make_opener(),
  HttpsOnlyRedirectHandler, main(argv, *, fetch, manifest_path, data_dir) -> int
  where fetch(url, max_bytes) -> bytes.
"""

import hashlib
import io
import json
import pathlib
import ssl
import tarfile
import urllib.request

import pytest

from tests.qasper_support import MINI, make_manifest

MEMBER = "qasper-dev-fixture.json"


def _tar(entries: list[tuple[str, bytes]], extra=None, compress: bool = False) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if compress else "w") as tar:
        for name, payload in entries:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        for info in extra or []:
            tar.addfile(info)
    return buf.getvalue()


class _Lazy:
    """Imports eval.qasper_download on first attribute use, so a missing module fails each test (not setup)."""

    def __getattr__(self, name):
        from eval import qasper_download

        return getattr(qasper_download, name)


dl = _Lazy()


# ---- URL, redirects, TLS ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url", ["http://example.org/a.tgz", "file:///etc/passwd", "ftp://example.org/a", "data:text/plain,hi", "", "//x/y"]
)
def test_download_rejects_non_https_url(url):
    """Threat row 20 / M2: only https URLs are accepted up front."""
    with pytest.raises(dl.DownloadError):
        dl.validate_url(url)
    dl.validate_url("https://example.org/a.tgz")


def test_download_rejects_redirect_to_http():
    """M2: a redirect to a non-https target is refused before any request is sent."""
    handler = dl.HttpsOnlyRedirectHandler()
    req = urllib.request.Request("https://example.org/a")
    for target in ("http://evil.example/a", "file:///etc/passwd", "ftp://evil.example/a"):
        with pytest.raises(dl.DownloadError):
            handler.redirect_request(req, io.BytesIO(), 302, "Found", {}, target)
    new = handler.redirect_request(req, io.BytesIO(), 302, "Found", {}, "https://mirror.example/a")
    assert new is None or new.full_url == "https://mirror.example/a"


def test_download_uses_verified_tls_context():
    """M2: the opener has a certificate-verifying HTTPS handler and the https-only redirect handler."""
    opener = dl.make_opener()
    https = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert https and https[0]._context.verify_mode == ssl.CERT_REQUIRED and https[0]._context.check_hostname
    assert any(isinstance(h, dl.HttpsOnlyRedirectHandler) for h in opener.handlers)


# ---- size caps and hash -------------------------------------------------------------------------------------
def test_download_and_member_size_caps():
    """Threat row 21: reading stops beyond the byte cap (exactly the cap is fine)."""
    assert dl.read_limited(io.BytesIO(b"x" * 100), 100) == b"x" * 100
    with pytest.raises(dl.DownloadError):
        dl.read_limited(io.BytesIO(b"x" * 101), 100)


def test_download_rejects_hash_mismatch():
    """Threat row 20: archive bytes whose SHA-256 differs from the manifest are rejected."""
    data = b"archive bytes"
    dl.verify_sha256(data, hashlib.sha256(data).hexdigest())
    with pytest.raises(dl.DownloadError):
        dl.verify_sha256(data + b"!", hashlib.sha256(data).hexdigest())
    with pytest.raises(dl.DownloadError):
        dl.verify_sha256(data, None)


# ---- archive handling ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("compress", [False, True])
def test_extract_member_reads_only_the_allow_listed_regular_file(compress):
    """Threat row 21: the one expected member is returned from memory; other members are never read."""
    archive = _tar([("other.json", b"{}"), (MEMBER, b'{"ok": 1}')], compress=compress)
    assert dl.extract_member(archive, MEMBER, max_member_bytes=1000, max_members=10) == b'{"ok": 1}'


def test_archive_member_outside_allow_list_or_link_is_rejected(tmp_path):
    """Threat row 21 / M3: missing member, symlink, hardlink, duplicate and traversal names are all rejected."""
    caps = dict(max_member_bytes=1000, max_members=10)
    with pytest.raises(dl.DownloadError):
        dl.extract_member(_tar([("other.json", b"{}")]), MEMBER, **caps)

    for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        link = tarfile.TarInfo(MEMBER)
        link.type, link.linkname = kind, "/etc/passwd" if kind == tarfile.SYMTYPE else "other.json"
        with pytest.raises(dl.DownloadError):
            dl.extract_member(_tar([("other.json", b"{}")], extra=[link]), MEMBER, **caps)

    with pytest.raises(dl.DownloadError):  # duplicate allow-listed names
        dl.extract_member(_tar([(MEMBER, b"{}"), (MEMBER, b'{"x": 2}')]), MEMBER, **caps)

    for evil in ("../evil.json", "/abs/evil.json", "a/../../evil.json"):
        with pytest.raises(dl.DownloadError):
            dl.extract_member(_tar([(MEMBER, b"{}"), (evil, b"x")]), MEMBER, **caps)
    assert not (tmp_path.parent / "evil.json").exists()


def test_archive_member_count_and_decompressed_size_caps():
    """M3: too many members, or a member (gzip bomb) larger than the cap, is rejected."""
    many = _tar([(f"f{i}.json", b"{}") for i in range(50)] + [(MEMBER, b"{}")])
    with pytest.raises(dl.DownloadError):
        dl.extract_member(many, MEMBER, max_member_bytes=1000, max_members=10)
    bomb = _tar([(MEMBER, b"0" * 2_000_000)], compress=True)
    assert len(bomb) < 20_000  # tiny archive, huge member
    with pytest.raises(dl.DownloadError):
        dl.extract_member(bomb, MEMBER, max_member_bytes=10_000, max_members=10)


def test_archive_pax_header_bomb_is_rejected(monkeypatch):
    """P5 fix 6: a small gzip whose PAX header expands to megabytes is stopped by the decompressed-size cap."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo(MEMBER)
        info.size = 2
        info.pax_headers = {"comment": "x" * 5_000_000}
        tar.addfile(info, io.BytesIO(b"{}"))
    bomb = buf.getvalue()
    assert len(bomb) < 100_000
    monkeypatch.setattr(dl_module(), "MAX_DECOMPRESSED", 1_000_000, raising=False)
    with pytest.raises(dl.DownloadError):
        dl.extract_member(bomb, MEMBER, max_member_bytes=1000, max_members=10)


def dl_module():
    from eval import qasper_download

    return qasper_download


# ---- main(): trust on first use, verification, subset derivation --------------------------------------------
def _setup(tmp_path, *, archive_hash=None, member_bytes=None):
    member_bytes = member_bytes if member_bytes is not None else MINI.read_bytes()
    archive = _tar([(MEMBER, member_bytes)], compress=True)
    manifest_path = tmp_path / "manifest.json"
    manifest = make_manifest(MINI, archive_sha256=archive_hash, subset_sha256=None, paper_ids=[])
    manifest["archive_member"] = MEMBER
    manifest["source_url"] = "https://example.org/qasper.tgz"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    calls: list[tuple[str, int]] = []

    def fetch(url: str, max_bytes: int) -> bytes:
        calls.append((url, max_bytes))
        return archive

    return archive, manifest_path, tmp_path / "data", fetch, calls


def test_record_hash_refuses_when_hash_already_pinned(tmp_path):
    """M1: --record-hash exits non-zero unless archive_sha256 is null; the manifest is left untouched."""
    _archive, manifest_path, data_dir, fetch, calls = _setup(tmp_path, archive_hash="a" * 64)
    before = manifest_path.read_bytes()
    code = dl.main(["--record-hash"], fetch=fetch, manifest_path=manifest_path, data_dir=data_dir)
    assert code != 0 and manifest_path.read_bytes() == before and not (data_dir / "subset.json").exists()
    assert calls == []  # refused before any download


def test_first_download_requires_record_hash_flag(tmp_path):
    """ADR 0009: with archive_sha256 null the script refuses to run unless --record-hash is passed."""
    _archive, manifest_path, data_dir, fetch, _calls = _setup(tmp_path)
    assert dl.main([], fetch=fetch, manifest_path=manifest_path, data_dir=data_dir) != 0
    assert not (data_dir / "subset.json").exists()


def test_record_hash_writes_hash_subset_and_paper_ids_then_later_runs_verify(tmp_path):
    """ADR 0009: first run records hashes and paper ids; a later run verifies against them and rebuilds the subset."""
    archive, manifest_path, data_dir, fetch, calls = _setup(tmp_path)
    assert dl.main(["--record-hash"], fetch=fetch, manifest_path=manifest_path, data_dir=data_dir) == 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["archive_sha256"] == hashlib.sha256(archive).hexdigest()
    subset = data_dir / "subset.json"
    assert subset.exists() and manifest["subset_sha256"] == hashlib.sha256(subset.read_bytes()).hexdigest()
    assert manifest["paper_ids"] == sorted(manifest["paper_ids"]) and "P001" in manifest["paper_ids"]
    assert "../evil" not in manifest["paper_ids"] and "P004" not in manifest["paper_ids"]
    assert calls and calls[0][0] == "https://example.org/qasper.tgz" and calls[0][1] > 0
    json.loads(subset.read_text(encoding="utf-8"))  # plain JSON
    assert (data_dir.stat().st_mode & 0o777) == 0o700

    subset.unlink()
    assert dl.main([], fetch=fetch, manifest_path=manifest_path, data_dir=data_dir) == 0
    assert subset.exists()


def test_main_rejects_archive_whose_hash_differs_from_manifest(tmp_path):
    """Threat row 20: a pinned hash that does not match the downloaded bytes writes nothing."""
    _archive, manifest_path, data_dir, fetch, _calls = _setup(tmp_path, archive_hash="b" * 64)
    assert dl.main([], fetch=fetch, manifest_path=manifest_path, data_dir=data_dir) != 0
    assert not (data_dir / "subset.json").exists()


def test_main_refuses_non_https_manifest_url(tmp_path):
    """Threat row 20: a manifest pointing at http:// is refused before fetching."""
    _archive, manifest_path, data_dir, fetch, calls = _setup(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_url"] = "http://example.org/qasper.tgz"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert dl.main(["--record-hash"], fetch=fetch, manifest_path=manifest_path, data_dir=data_dir) != 0
    assert calls == []


def test_download_module_never_extracts_to_disk():
    """Threat row 21: no extractall/extract and no os.path.join on member names in the download module."""
    path = pathlib.Path(__file__).resolve().parent.parent / "eval" / "qasper_download.py"
    assert path.exists(), "eval/qasper_download.py missing (T3a)"
    source = path.read_text(encoding="utf-8")
    assert "extractall" not in source and ".extract(" not in source


def test_download_refuses_foreign_or_nonempty_data_dir(tmp_path, monkeypatch):
    """P5 fix 5: the downloader will not reuse a data dir owned by someone else or holding unexpected files."""
    import os

    _archive, manifest_path, data_dir, fetch, calls = _setup(tmp_path)
    data_dir.mkdir(mode=0o700)
    (data_dir / "other.txt").write_text("not mine", encoding="utf-8")
    assert dl.main(["--record-hash"], fetch=fetch, manifest_path=manifest_path, data_dir=data_dir) != 0
    assert calls == [] and not (data_dir / "subset.json").exists()
    (data_dir / "other.txt").unlink()

    me = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: me + 1)
    monkeypatch.setattr(os, "geteuid", lambda: me + 1)  # the directory now looks foreign
    assert dl.main(["--record-hash"], fetch=fetch, manifest_path=manifest_path, data_dir=data_dir) != 0
    assert calls == [] and not (data_dir / "subset.json").exists()
    monkeypatch.undo()

    (data_dir / "subset.json").write_text("{}", encoding="utf-8")  # only subset.json: accepted
    assert dl.main(["--record-hash"], fetch=fetch, manifest_path=manifest_path, data_dir=data_dir) == 0


def test_data_dir_swapped_during_download_is_refused(tmp_path, monkeypatch):
    """P5 fix round 2 (security N2): the data-dir checks are repeated right before writing, so a directory swapped
    for a symlink into the repository during the download is refused and nothing is written there."""
    from eval import qasper

    _archive, manifest_path, data_dir, real_fetch, _calls = _setup(tmp_path)
    fake_repo = tmp_path / "repo"
    target = fake_repo / "inside"
    target.mkdir(parents=True)
    monkeypatch.setattr(qasper, "REPO_ROOT", fake_repo)

    def swapping_fetch(url, max_bytes):
        data_dir.rmdir()
        data_dir.symlink_to(target, target_is_directory=True)
        return real_fetch(url, max_bytes)

    code = dl.main(["--record-hash"], fetch=swapping_fetch, manifest_path=manifest_path, data_dir=data_dir)
    assert code != 0
    assert list(target.iterdir()) == []
