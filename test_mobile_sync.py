"""Tests for soundboard/mobile_sync.py (Phase 0 of the mobile companion).

Run from the repo root:  .venv/Scripts/python.exe test_mobile_sync.py

Covers: the §5.1 reference-set walk (tabs + persons + favorites, all three
path fields), dedupe, dual relative+absolute references to one file (the
rewrite-gating regression), external audio/avatar rewrites, excluded-dir
hardening, dangling-ref tolerance, hash-cache hit + invalidation, zip
round-trip (Hebrew/bracket names + UTF-8 flag + §6.1 exclusions), and the
LAN server end-to-end (token on every route, by-index bytes, 404, 410 after
a source vanishes, /complete shutdown, idle timeout).
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import soundboard.mobile_sync as msync
from soundboard.mobile_sync import (
    FORMAT_VERSION,
    HashCache,
    PackServer,
    attach_app_info,
    build_pack,
    collect_references,
    export_zip,
)

HEBREW_WAV = "קללות [רמיקס] (חתוך)_ab12cd34.wav"


def make_fixture(root: Path):
    """A tiny fake workspace exercising every §5.1/§6.2 edge."""
    (root / "sounds").mkdir(parents=True)
    (root / "images").mkdir()
    (root / "audio_cache").mkdir()
    outside = root / "outside"
    outside.mkdir()

    (root / "sounds" / HEBREW_WAV).write_bytes(b"RIFF" + b"\x01" * 500)
    (root / "sounds" / "plain_00000001.mp3").write_bytes(b"ID3" + b"\x02" * 300)
    (root / "sounds" / "original_source.wav").write_bytes(b"RIFF" + b"\x03" * 400)
    (root / "sounds" / "fav_cut_00000002.wav").write_bytes(b"RIFF" + b"\x06" * 350)
    (root / "images" / "thumb_aa11bb22.png").write_bytes(b"\x89PNG" + b"\x04" * 200)
    (outside / "avatar pic.png").write_bytes(b"\x89PNG" + b"\x05" * 250)
    (outside / "ext audio [clip].wav").write_bytes(b"RIFF" + b"\x07" * 450)
    (root / "audio_cache" / "deadbeef.npy").write_bytes(b"\x93NUMPY" + b"\x08" * 100)

    config = {
        "tabs": [
            {
                "name": "Main",
                "emoji": None,
                "color": "#5865F2",
                "slots": {
                    # sparse string keys on purpose
                    "1": {
                        "name": "קללה",
                        "file_path": "sounds\\" + HEBREW_WAV,
                        "image_path": "images\\thumb_aa11bb22.png",
                        "source_file_path": "sounds\\original_source.wav",
                        "volume": 2.0,
                    },
                    "4": {
                        "name": "dup ref",  # same audio as slot 1 → dedupe
                        "file_path": "sounds/" + HEBREW_WAV,
                        "volume": 0.5,
                    },
                    "7": {
                        "name": "dangling",
                        "file_path": "sounds\\gone_forever.wav",
                    },
                    "9": {
                        # ABSOLUTE ref to a file also referenced relatively —
                        # regression for the rewrite-gating bug: must be
                        # repointed to the existing safe relative path.
                        "name": "abs dup",
                        "file_path": str(root / "sounds" / HEBREW_WAV),
                    },
                },
            }
        ],
        "persons": [
            {
                "name": "Tal",
                "image_path": str(outside / "avatar pic.png"),  # absolute!
                "groups": [
                    {
                        "id": "g1",
                        "name": "Cuts",
                        "sounds": [
                            {"name": "chip", "file_path": "sounds\\plain_00000001.mp3"},
                            {
                                # absolute OUT-OF-TREE audio (person "From file…")
                                "name": "ext",
                                "file_path": str(outside / "ext audio [clip].wav"),
                            },
                            {
                                # absolute ref into audio_cache → must be EXCLUDED
                                "name": "cache poison",
                                "file_path": str(root / "audio_cache" / "deadbeef.npy"),
                            },
                        ],
                    }
                ],
            }
        ],
        "favorites_board": {
            "name": "Favorites",
            "image_path": None,
            "groups": [
                {
                    "id": "f1",
                    "name": "Best",
                    "sounds": [
                        {
                            "name": "fav",
                            "file_path": "sounds\\fav_cut_00000002.wav",
                            "source_file_path": "sounds\\original_source.wav",
                            "image_path": "images\\thumb_aa11bb22.png",
                        }
                    ],
                }
            ],
        },
        "custom_groups": ["קללות"],
        "ptt_key": "f9",  # desktop-only key must round-trip untouched
    }
    return config


def test_collect_and_build():
    tmp = Path(tempfile.mkdtemp(prefix="lsb_msync_"))
    try:
        config = make_fixture(tmp)
        manifest, sources = build_pack(config, tmp)

        assert manifest["format_version"] == FORMAT_VERSION
        files = manifest["files"]
        paths = [f["path"] for f in files]
        emb = manifest["config"]

        # 7 unique packable files: hebrew wav (3 refs deduped), plain mp3,
        # original_source.wav (2 refs), fav_cut wav, thumb png (2 refs),
        # external avatar png, external audio wav
        assert len(files) == 7, paths
        assert len(sources) == 7
        assert all("\\" not in p for p in paths), paths
        assert [f["i"] for f in files] == list(range(7))
        assert "sounds/" + HEBREW_WAV in paths
        assert "sounds/original_source.wav" in paths  # source_file_path is a ref
        assert "sounds/fav_cut_00000002.wav" in paths  # favorites are walked

        # §6.1 exclusions are structural — nothing from cache/backup dirs
        assert not any(p.startswith(("audio_cache", "config_backups")) for p in paths)
        assert not any(p.endswith((".bak", ".log", ".npy")) for p in paths)
        cache_skips = [s for s in manifest["skipped"] if s["reason"] == "excluded"]
        assert len(cache_skips) == 1 and "audio_cache" in cache_skips[0]["path"]

        # external avatar + external audio got content-addressed homes
        avatar = [p for p in paths if p.startswith("images/avatars/")]
        ext_audio = [p for p in paths if p.startswith("sounds/external/")]
        assert len(avatar) == 1 and len(ext_audio) == 1, paths
        assert emb["persons"][0]["image_path"] == avatar[0]
        assert emb["persons"][0]["groups"][0]["sounds"][1]["file_path"] == ext_audio[0]
        # the ORIGINAL config dict is untouched (build_pack deep-copies)
        assert config["persons"][0]["image_path"].endswith("avatar pic.png")

        # REGRESSION (review finding #1): the absolute ref to an in-tree file
        # is repointed to the existing safe relative path — never left
        # absolute, never relocated to sounds/external/.
        assert emb["tabs"][0]["slots"]["9"]["file_path"] == "sounds/" + HEBREW_WAV

        # dangling ref skipped, not fatal, recorded once
        missing = [s for s in manifest["skipped"] if s["reason"] == "missing-on-disk"]
        assert missing == [{"path": "sounds/gone_forever.wav",
                            "reason": "missing-on-disk"}]

        # desktop-only keys + sparse slot keys survive verbatim
        assert emb["ptt_key"] == "f9"
        assert set(emb["tabs"][0]["slots"].keys()) == {"1", "4", "7", "9"}

        # totals add up
        assert manifest["totals"]["files"] == 7
        assert manifest["totals"]["bytes"] == sum(f["size"] for f in files)

        # hash cache: second build hits the cache and agrees
        cache = HashCache(tmp)
        manifest2, _ = build_pack(config, tmp, hash_cache=cache)
        assert [f["md5"] for f in manifest2["files"]] == [f["md5"] for f in files]
        assert (tmp / "mobile_sync_hashcache.json").is_file()

        # hash cache INVALIDATION: change a file's bytes → md5 must change
        target = tmp / "sounds" / "plain_00000001.mp3"
        old_md5 = next(f["md5"] for f in files if f["path"].endswith(".mp3"))
        time.sleep(0.01)  # ensure a distinct mtime_ns
        target.write_bytes(b"ID3" + b"\x99" * 333)
        manifest3, sources3 = build_pack(config, tmp, hash_cache=HashCache(tmp))
        new_md5 = next(f["md5"] for f in manifest3["files"]
                       if f["path"].endswith(".mp3"))
        assert new_md5 != old_md5

        print("  ok: collect/build/dedupe/rewrites/exclusions/skip/cache")
        return manifest3, sources3, tmp
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def test_zip_roundtrip(manifest, sources, tmp: Path):
    import zipfile

    dest = tmp / "pack.zip"
    export_zip(manifest, sources, dest)
    with zipfile.ZipFile(dest) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        loaded = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert loaded["totals"] == manifest["totals"]
        for entry, src in zip(manifest["files"], sources):
            assert entry["path"] in names, entry["path"]
            assert zf.read(entry["path"]) == src.read_bytes()
        # §6.5: non-ASCII names MUST carry the zip UTF-8 flag (bit 0x800) —
        # third-party readers on the phone rely on it for the Hebrew names.
        for info in zf.infolist():
            if any(ord(c) > 127 for c in info.filename):
                assert info.flag_bits & 0x800, info.filename
        # §6.1: nothing excluded ever enters a pack
        assert not any(n.startswith(("audio_cache", "config_backups"))
                       for n in names)
        assert not any(n.endswith((".bak", ".log", ".npy")) for n in names)
    print("  ok: zip round-trip (Hebrew/brackets, UTF-8 flag, exclusions)")


def _get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def _expect_http_error(url, code, method="GET"):
    try:
        req = urllib.request.Request(url, method=method)
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == code, f"expected {code}, got {e.code}"
        return
    raise AssertionError(f"expected HTTP {code} for {method} {url}")


def test_server(manifest, sources):
    events = []
    srv = PackServer(manifest, sources, port=0,
                     on_event=lambda ev, **kw: events.append((ev, kw)))
    srv.start()
    base = f"http://127.0.0.1:{srv.port}"
    tok = srv.token

    # token required on EVERY route (§6.4)
    _expect_http_error(f"{base}/v1/manifest", 403)
    _expect_http_error(f"{base}/v1/manifest?token=WRONG", 403)
    _expect_http_error(f"{base}/v1/file/0", 403)
    _expect_http_error(f"{base}/v1/complete", 403, method="POST")
    # non-ASCII token junk → clean 403, not a dropped connection
    _expect_http_error(f"{base}/v1/manifest?token=%D7%90%D7%91", 403)

    status, body = _get(f"{base}/v1/manifest?token={tok}")
    assert status == 200
    served = json.loads(body.decode("utf-8"))
    assert served["totals"] == manifest["totals"]
    assert served["config"]["ptt_key"] == "f9"

    # every file byte-identical, served by index (Hebrew never in the URL)
    for entry, src in zip(manifest["files"], sources):
        status, body = _get(f"{base}/v1/file/{entry['i']}?token={tok}")
        assert status == 200 and body == src.read_bytes(), entry["path"]

    _expect_http_error(f"{base}/v1/file/999?token={tok}", 404)

    # 410 for a source that vanished after the manifest froze its path
    victim_idx = len(sources) - 1
    victim_backup = sources[victim_idx].read_bytes()
    sources[victim_idx].unlink()
    _expect_http_error(f"{base}/v1/file/{victim_idx}?token={tok}", 410)
    sources[victim_idx].write_bytes(victim_backup)

    # /complete → 200, then the server shuts itself down
    req = urllib.request.Request(f"{base}/v1/complete?token={tok}", method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", srv.port), timeout=0.3).close()
            time.sleep(0.1)
        except OSError:
            break
    else:
        raise AssertionError("server did not stop after /complete")
    kinds = [e[0] for e in events]
    assert "complete" in kinds and "stopped" in kinds, kinds
    print("  ok: server (token everywhere, by-index bytes, 404/410, /complete)")


def test_browser_bootstrap(manifest, sources, tmp: Path):
    """USB-free path: landing page + APK serving + background zip bake."""
    import zipfile

    fake_apk = tmp / "app-debug.apk"
    fake_apk.write_bytes(b"PK\x03\x04fakeapk" + b"\x0a" * 300)
    events = []
    srv = PackServer(manifest, sources, port=0, apk_path=fake_apk,
                     on_event=lambda ev, **kw: events.append((ev, kw)))
    srv.start()
    base = f"http://127.0.0.1:{srv.port}"
    tok = srv.token

    _expect_http_error(f"{base}/", 403)
    status, body = _get(f"{base}/?token={tok}")
    html = body.decode("utf-8")
    assert status == 200
    assert f"/v1/apk?token={tok}" in html and f"/v1/zip?token={tok}" in html

    # APK: byte-identical, with the content type that triggers Android's
    # install flow after download
    with urllib.request.urlopen(f"{base}/v1/apk?token={tok}", timeout=5) as r:
        assert r.headers.get("Content-Type") == "application/vnd.android.package-archive"
        assert r.read() == fake_apk.read_bytes()

    # zip: baked in the background on landing hit, valid, totals intact
    status, body = _get(f"{base}/v1/zip?token={tok}", timeout=30)
    assert status == 200
    zpath = tmp / "served.zip"
    zpath.write_bytes(body)
    with zipfile.ZipFile(zpath) as zf:
        loaded = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert loaded["totals"] == manifest["totals"]

    srv.request_stop("test-done")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and srv.zip_path is not None \
            and srv.zip_path.exists():
        time.sleep(0.1)
    assert not (srv.zip_path is not None and srv.zip_path.exists()), \
        "baked zip not cleaned up on stop"

    # a server with no APK configured answers 404, not a crash
    srv2 = PackServer(manifest, sources, port=0)
    srv2.start()
    _expect_http_error(f"http://127.0.0.1:{srv2.port}/v1/apk?token={srv2.token}", 404)
    srv2.request_stop("test-done")
    print("  ok: browser bootstrap (landing, apk, zip bake+serve+cleanup)")


def test_app_info(manifest, tmp: Path):
    """manifest `app` block: version from output-metadata.json + apk md5."""
    apk_dir = tmp / "mobile/android/app/build/outputs/apk/debug"
    apk_dir.mkdir(parents=True)
    apk_bytes = b"PK\x03\x04versioned-apk" + b"\x0b" * 200
    (apk_dir / "app-debug.apk").write_bytes(apk_bytes)
    (apk_dir / "output-metadata.json").write_text(json.dumps({
        "elements": [{"versionCode": 7, "versionName": "0.7.0",
                      "outputFile": "app-debug.apk"}]
    }), encoding="utf-8")

    import copy
    import hashlib
    m = copy.deepcopy(manifest)
    attach_app_info(m, tmp)
    app = m["app"]
    assert app["version_code"] == 7 and app["version_name"] == "0.7.0"
    assert app["apk_md5"] == hashlib.md5(apk_bytes).hexdigest()
    assert app["apk_size"] == len(apk_bytes)

    # no APK → no app block, never an error
    m2 = copy.deepcopy(manifest)
    attach_app_info(m2, tmp / "outside")
    assert "app" not in m2
    print("  ok: app-info block (version/md5, absent when unbuilt)")


def test_idle_timeout(manifest, sources):
    old_idle, old_poll = msync.IDLE_TIMEOUT_S, msync._WATCHDOG_POLL_S
    msync.IDLE_TIMEOUT_S, msync._WATCHDOG_POLL_S = 0.2, 0.05
    try:
        events = []
        srv = PackServer(manifest, sources, port=0,
                         on_event=lambda ev, **kw: events.append((ev, kw)))
        srv.start()
        # Wait for the async "stopped" event itself — srv.stopped flips
        # before the shutdown thread finishes and emits it.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any(
                e[0] == "stopped" for e in events):
            time.sleep(0.05)
        assert srv.stopped, "idle watchdog never fired"
        assert ("stopped", {"reason": "timeout"}) in events, events
    finally:
        msync.IDLE_TIMEOUT_S, msync._WATCHDOG_POLL_S = old_idle, old_poll
    print("  ok: idle timeout self-stop")


def test_real_library():
    """Read-only sanity walk over the real config, when running in the repo."""
    cfg_path = Path("soundboard_config.json")
    if not cfg_path.is_file() or not Path("sounds").is_dir():
        print("  skip: real library not present")
        return
    with open(cfg_path, encoding="utf-8") as f:
        config = json.load(f)
    refs, skipped = collect_references(config, Path.cwd())
    audio = [r for r in refs.values() if r.kind == "audio"]
    images = [r for r in refs.values() if r.kind == "image"]
    assert 400 <= len(audio) <= 800, len(audio)
    assert 50 <= len(images) <= 300, len(images)
    assert len(skipped) <= 10, skipped
    assert not any("audio_cache" in str(r.src) for r in refs.values())
    for r in refs.values():
        if not r.external:
            assert r.pack_path.startswith(("sounds/", "images/")), r.pack_path
    externals = [r for r in refs.values() if r.external]
    print(f"  ok: real library — {len(audio)} audio + {len(images)} image refs, "
          f"{len(externals)} external, {len(skipped)} dangling skipped")


def main():
    print("test_mobile_sync:")
    manifest, sources, tmp = test_collect_and_build()
    try:
        test_zip_roundtrip(manifest, sources, tmp)
        test_server(manifest, sources)
        test_browser_bootstrap(manifest, sources, tmp)
        test_app_info(manifest, tmp)
        test_idle_timeout(manifest, sources)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    test_real_library()
    print("ALL PASS")


if __name__ == "__main__":
    main()
