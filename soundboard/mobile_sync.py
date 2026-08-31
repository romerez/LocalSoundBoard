"""SoundPack export + LAN transfer server for the Android companion app.

Implements the desktop half of the transfer contract in ``mobile/README.md``
(§5 reference-set, §6 SoundPack format / Wi-Fi protocol, format_version 1):

* :func:`collect_references` — walk the config's path-bearing fields
  (tabs / persons / favorites, incl. ``source_file_path`` and avatars).
* :func:`build_pack` — manifest with per-file md5 (mtime-cached in
  ``mobile_sync_hashcache.json``), absolute/out-of-tree paths rewritten into
  the pack, dangling refs skipped-and-recorded, source paths frozen.
* :func:`export_zip` — ``manifest.json`` + files, mirroring ``files[].path``.
* :class:`PackServer` — token-protected LAN HTTP server; files are served by
  manifest INDEX (``/v1/file/{i}``) so Hebrew/bracket names never touch URLs.
* :class:`MobileSyncDialog` — the "📱 Send to Phone" QR dialog.

Never exports: audio_cache/, config_backups/, *.bak, debug logs, orphans.
Schema or naming changes here must stay in lockstep with mobile/README.md §6
(bump FORMAT_VERSION together with the doc).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger("soundboard")

FORMAT_VERSION = 1
DEFAULT_PORT = 8765
IDLE_TIMEOUT_S = 600  # server self-stops after 10 min without a request
HASH_CACHE_FILE = "mobile_sync_hashcache.json"

# Debug APK produced by the Phase-1 Android build — served to the phone's
# browser for USB-free first-time install (scan QR with the camera app).
APK_RELATIVE_PATH = Path("mobile/android/app/build/outputs/apk/debug/app-debug.apk")


def find_apk(root: Path) -> Optional[Path]:
    p = root / APK_RELATIVE_PATH
    return p if p.is_file() else None


def read_apk_info(root: Path) -> Optional[dict]:
    """App-version block for the manifest (mobile/README.md §6.3 `app`).

    versionCode/versionName come from the Gradle build's
    output-metadata.json next to the APK; the phone compares version_code
    against its own BuildConfig.VERSION_CODE to offer in-app updates —
    bump versionCode in mobile/android/app/build.gradle.kts on every
    phone release or the phone will never see the update.
    """
    apk = find_apk(root)
    if apk is None:
        return None
    version_code, version_name = 0, "?"
    try:
        meta = json.loads((apk.parent / "output-metadata.json").read_text(encoding="utf-8"))
        element = (meta.get("elements") or [{}])[0]
        version_code = int(element.get("versionCode") or 0)
        version_name = str(element.get("versionName") or "?")
    except Exception:
        logger.exception("mobile_sync: output-metadata.json unreadable")
    h = hashlib.md5()
    with open(apk, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return {
        "version_code": version_code,
        "version_name": version_name,
        "apk_md5": h.hexdigest(),
        "apk_size": apk.stat().st_size,
    }


def attach_app_info(manifest: dict, root: Path) -> None:
    """Add the `app` block when a built APK exists (no-op otherwise)."""
    info = read_apk_info(root)
    if info:
        manifest["app"] = info

# Containers already compressed (or huge) — ZIP_STORED for export speed.
_STORED_EXTS = {".wav", ".mp3", ".ogg", ".m4a", ".opus", ".webm", ".flac", ".aac", ".wma"}

_AUDIO_FIELDS = ("file_path", "source_file_path")
_IMAGE_FIELD = "image_path"


# =============================================================================
# Reference collection (mobile/README.md §5.1 reference-set)
# =============================================================================

@dataclass
class _Ref:
    """One unique source file referenced by the config."""

    src: Path                    # resolved absolute path on disk
    kind: str                    # "audio" | "image"
    pack_path: Optional[str]     # safe relative path, or None until build assigns one
    rewrites: list = field(default_factory=list)  # (dict, field) occurrences to repoint

    @property
    def external(self) -> bool:
        """No safe in-tree relative path referenced this file (yet)."""
        return self.pack_path is None


def _iter_slot_dicts(config: dict):
    """Yield every SoundSlot dict: tab slots, person-group sounds, favorites."""
    for tab in config.get("tabs") or []:
        slots = tab.get("slots") or {}
        if isinstance(slots, dict):
            yield from (s for s in slots.values() if isinstance(s, dict))
    persons = list(config.get("persons") or [])
    fav = config.get("favorites_board")
    if isinstance(fav, dict):
        persons.append(fav)
    for person in persons:
        for group in person.get("groups") or []:
            yield from (s for s in (group.get("sounds") or []) if isinstance(s, dict))


def _iter_person_dicts(config: dict):
    yield from (p for p in config.get("persons") or [] if isinstance(p, dict))
    fav = config.get("favorites_board")
    if isinstance(fav, dict):
        yield fav


def _norm_ref(raw: str) -> str:
    """Normalize a config path reference: backslashes → slashes, strip."""
    return raw.replace("\\", "/").strip()


def _is_safe_relative(norm: str) -> bool:
    """True when the ref can keep its own path inside the pack verbatim."""
    if os.path.isabs(norm) or (len(norm) > 1 and norm[1] == ":"):
        return False
    if ".." in norm.split("/"):
        return False
    return norm.startswith("sounds/") or norm.startswith("images/")


_EXCLUDED_DIRS = ("audio_cache", "config_backups")


def _is_excluded_source(src: Path, root: Path) -> bool:
    """§6.1 hardening: never pack cache/backup/log content, even when a
    pathological config references it by absolute path."""
    name = src.name.lower()
    if name.endswith(".bak") or name.startswith("debug.log"):
        return True
    try:
        rel = src.relative_to(root)
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0].lower() in _EXCLUDED_DIRS


def collect_references(config: dict, root: Path):
    """Walk the reference-set; return (refs_by_key, skipped, rewrite_count).

    ``config`` should be the deep copy that will be embedded in the manifest —
    rewrite targets are recorded against it directly.
    """
    refs: dict[str, _Ref] = {}
    skipped: list[dict] = []
    skipped_seen: set[str] = set()

    def skip(norm: str, reason: str):
        if norm not in skipped_seen:
            skipped_seen.add(norm)
            skipped.append({"path": norm, "reason": reason})

    def add(container: dict, fld: str, kind: str):
        raw = container.get(fld)
        if not raw or not isinstance(raw, str):
            return
        norm = _norm_ref(raw)
        if not norm:
            return
        safe = _is_safe_relative(norm)
        src = (root / norm) if safe else Path(norm)
        try:
            src = src.resolve()
            missing = not src.is_file()
        except OSError:
            missing = True
        if missing:
            skip(norm, "missing-on-disk")
            return
        if not safe and _is_excluded_source(src, root):
            skip(norm, "excluded")
            return
        key = os.path.normcase(str(src))
        ref = refs.get(key)
        if ref is None:
            ref = refs[key] = _Ref(src=src, kind=kind, pack_path=None)
        # Safety is a property of THIS occurrence, not of the deduped file:
        # the same file may be referenced both relatively and absolutely.
        # Any safe occurrence donates its relative path as the pack home;
        # every unsafe occurrence gets repointed to that home at build time.
        if safe and ref.pack_path is None:
            ref.pack_path = norm
        if not safe:
            ref.rewrites.append((container, fld))

    for slot in _iter_slot_dicts(config):
        for fld in _AUDIO_FIELDS:
            add(slot, fld, "audio")
        add(slot, _IMAGE_FIELD, "image")
    for person in _iter_person_dicts(config):
        add(person, _IMAGE_FIELD, "image")

    return refs, skipped


# =============================================================================
# md5 hash cache (path+mtime+size keyed; rebuilding 660MB of hashes is slow)
# =============================================================================

class HashCache:
    def __init__(self, root: Path):
        self.path = root / HASH_CACHE_FILE
        self._data: dict = {}
        self._dirty = False
        try:
            with open(self.path, encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception:
            self._data = {}

    def md5(self, src: Path) -> str:
        st = src.stat()
        key = os.path.normcase(str(src))
        entry = self._data.get(key)
        if entry and entry.get("mtime") == st.st_mtime_ns and entry.get("size") == st.st_size:
            return entry["md5"]
        h = hashlib.md5()
        with open(src, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        digest = h.hexdigest()
        self._data[key] = {"mtime": st.st_mtime_ns, "size": st.st_size, "md5": digest}
        self._dirty = True
        return digest

    def save(self):
        if not self._dirty:
            return
        try:
            tmp = str(self.path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            os.replace(tmp, self.path)
            self._dirty = False
        except Exception:
            logger.exception("mobile_sync: hash cache save failed")


# =============================================================================
# Pack building (manifest + frozen source list)
# =============================================================================

def _desktop_version(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode == 0 and out.stdout.strip():
            return f"git:{out.stdout.strip()}"
    except Exception:
        pass
    return "unknown"


def build_pack(config: dict, root: Path, progress: Optional[Callable] = None,
               hash_cache: Optional[HashCache] = None):
    """Build (manifest_dict, sources) from a config dict.

    ``sources`` is the frozen list of absolute paths, index-aligned with
    ``manifest["files"]`` — the server serves from it, so files renamed after
    the build 404/mismatch instead of silently serving different content.
    ``config`` is deep-copied; the caller's dict is never modified.
    """
    config = json.loads(json.dumps(config))  # deep copy; manifest embeds this
    cache = hash_cache or HashCache(root)
    refs, skipped = collect_references(config, root)

    ordered = sorted(refs.values(), key=lambda r: (r.kind, str(r.src).lower()))
    used_paths = {r.pack_path for r in ordered if r.pack_path is not None}
    total = len(ordered)
    files, sources = [], []
    total_bytes = 0
    for done, ref in enumerate(ordered, start=1):
        if progress:
            progress("hash", done, total, ref.src.name)
        try:
            digest = cache.md5(ref.src)
            size = ref.src.stat().st_size
        except OSError:
            skipped.append({"path": ref.pack_path or str(ref.src), "reason": "unreadable"})
            continue
        if ref.pack_path is None:
            # Out-of-tree reference (abs-path avatars, person "From file…"):
            # give it a content-addressed home inside the pack
            # (mobile/README.md §6.2), disambiguated on the vanishingly-rare
            # md5-prefix collision with any other pack path.
            sub = "images/avatars" if ref.kind == "image" else "sounds/external"
            stem, ext = f"{sub}/{digest[:8]}", ref.src.suffix.lower()
            candidate, n = f"{stem}{ext}", 2
            while candidate in used_paths:
                candidate, n = f"{stem}-{n}{ext}", n + 1
            ref.pack_path = candidate
        used_paths.add(ref.pack_path)
        # Repoint every unsafe occurrence (absolute / out-of-tree) at the
        # pack home — which is the existing safe relative path when the same
        # file was also referenced safely somewhere else.
        for container, fld in ref.rewrites:
            container[fld] = ref.pack_path
        files.append({"i": len(files), "path": ref.pack_path, "size": size, "md5": digest})
        sources.append(ref.src)
        total_bytes += size

    cache.save()
    manifest = {
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "desktop_version": _desktop_version(root),
        "config": config,
        "files": files,
        "skipped": skipped,
        "totals": {"files": len(files), "bytes": total_bytes},
    }
    return manifest, sources


def export_zip(manifest: dict, sources: list, dest: Path,
               progress: Optional[Callable] = None):
    """Write a SoundPack zip: manifest.json + files mirroring files[].path."""
    files = manifest["files"]
    with zipfile.ZipFile(dest, "w", allowZip64=True) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=1),
            compress_type=zipfile.ZIP_DEFLATED,
        )
        for entry, src in zip(files, sources):
            if progress:
                progress("zip", entry["i"] + 1, len(files), entry["path"])
            ext = os.path.splitext(entry["path"])[1].lower()
            ctype = zipfile.ZIP_STORED if ext in _STORED_EXTS else zipfile.ZIP_DEFLATED
            zf.write(src, entry["path"], compress_type=ctype)


# =============================================================================
# LAN discovery + QR
# =============================================================================

def get_lan_ips() -> list[str]:
    """Candidate IPv4s, default-route interface first (UDP-connect trick)."""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 80))  # no packets sent; just picks a route
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith(("127.", "169.254.")):
                ips.append(ip)
    except Exception:
        pass
    return ips or ["127.0.0.1"]


def make_qr_image(data: str, target_px: int = 360):
    """PIL image QR for ``data``; None when the qrcode package is missing.

    Modules are integer-upscaled (NEAREST) and the result is padded with white
    to EXACTLY ``target_px`` square, so a caller can pass the DEVICE pixel size
    and wrap it in ``CTkImage(size=logical)`` without any resampling — the QR
    stays pixel-sharp on HiDPI. (Only a raw QR already wider than ``target_px``
    is returned unpadded.)
    """
    try:
        import qrcode  # lazy: only the 📱 dialog needs it
    except ImportError:
        return None
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    from PIL import Image
    scale = max(1, target_px // img.size[0])
    img = img.resize((img.size[0] * scale, img.size[1] * scale), Image.NEAREST)
    w, h = img.size
    if w < target_px or h < target_px:
        side = max(target_px, w, h)
        padded = Image.new("RGB", (side, side), "white")
        padded.paste(img, ((side - w) // 2, (side - h) // 2))
        img = padded
    return img


# =============================================================================
# LAN server (mobile/README.md §6.4)
# =============================================================================

class _Handler(BaseHTTPRequestHandler):
    server_version = "LSBMobileSync/1"
    protocol_version = "HTTP/1.1"
    server: "PackServer"  # narrows the BaseServer annotation for type checkers

    def log_message(self, fmt, *args):  # keep HTTP chatter out of stderr
        logger.debug("mobile_sync http: " + fmt, *args)

    # -- helpers ----------------------------------------------------------
    def _authed(self) -> bool:
        query = parse_qs(urlsplit(self.path).query)
        token = (query.get("token") or [""])[0]
        try:
            # bytes compare: compare_digest raises on non-ASCII str input,
            # and any LAN prober can send non-ASCII query junk.
            return hmac.compare_digest(token.encode("utf-8"),
                                       self.server.token.encode("ascii"))
        except Exception:
            return False

    def _send_json(self, code: int, payload: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _route(self) -> tuple[str, Optional[int]]:
        path = urlsplit(self.path).path.rstrip("/")
        if path == "":  # "/" — browser landing page (camera-app QR scan)
            return "landing", None
        if path == "/v1/manifest":
            return "manifest", None
        if path.startswith("/v1/file/"):
            try:
                return "file", int(path.rsplit("/", 1)[1])
            except ValueError:
                return "", None
        if path == "/v1/apk":
            return "apk", None
        if path == "/v1/zip":
            return "zip", None
        if path == "/v1/complete":
            return "complete", None
        return "", None

    # -- verbs ------------------------------------------------------------
    def do_GET(self):
        self.server.touch()
        if not self._authed():
            self._send_json(403, b'{"error": "bad token"}')
            return
        route, idx = self._route()
        if route == "landing":
            # A browser is here → the user is on the USB-free bootstrap
            # path; start baking the zip so it's ready when they tap it.
            self.server.emit("request", what="landing")
            self.server.ensure_zip_build()
            body = self.server.landing_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif route == "manifest":
            self.server.emit("request", what="manifest")
            self._send_json(200, self.server.manifest_bytes)
        elif route == "file" and idx is not None and 0 <= idx < len(self.server.sources):
            self._send_file(idx)
        elif route == "apk":
            apk = self.server.apk_path
            if apk is None or not apk.is_file():
                self._send_json(404, b'{"error": "apk not built"}')
                return
            if self._stream_path(apk, "application/vnd.android.package-archive",
                                 "LSB-Mobile.apk"):
                self.server.emit("download", what="apk")
        elif route == "zip":
            self.server.ensure_zip_build()
            self.server.zip_ready.wait(timeout=900)
            path = self.server.zip_path
            if path is None or not path.is_file():
                self._send_json(500, b'{"error": "zip build failed"}')
                return
            name = f"soundpack_{datetime.now():%Y%m%d}.zip"
            if self._stream_path(path, "application/zip", name):
                self.server.emit("download", what="zip")
        else:
            self._send_json(404, b'{"error": "not found"}')

    def _stream_path(self, path: Path, ctype: str, download_name: str) -> bool:
        """Stream a whole file with Content-Length; True when fully sent."""
        try:
            size = path.stat().st_size
            f = open(path, "rb")
        except OSError:
            self._send_json(410, b'{"error": "gone"}')
            return False
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{download_name}"')
            self.end_headers()
            sent = 0
            while sent < size:
                chunk = f.read(min(1024 * 1024, size - sent))
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
                self.server.touch()  # a long download is not "idle"
        finally:
            f.close()
        if sent < size:
            self.close_connection = True
            return False
        return True

    def do_POST(self):
        self.server.touch()
        # Drain any request body first — unread bytes desync the next
        # keep-alive request on this connection.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 0:
            self.rfile.read(min(length, 1024 * 1024))
        if not self._authed():
            self._send_json(403, b'{"error": "bad token"}')
            return
        route, _ = self._route()
        if route == "complete":
            self._send_json(200, b'{"ok": true}')
            self.server.emit("complete")
            self.server.request_stop("complete")
        else:
            self._send_json(404, b'{"error": "not found"}')

    def _send_file(self, idx: int):
        src = self.server.sources[idx]
        entry = self.server.manifest["files"][idx]
        try:
            size = src.stat().st_size
            f = open(src, "rb")
        except OSError:
            # Renamed/deleted since the manifest froze its path — the phone
            # records it like manifest.skipped and moves on (§6.4 failure
            # semantics); a 410 is clearer than serving wrong bytes.
            self._send_json(410, b'{"error": "gone"}')
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            sent = 0
            # Cap at the declared Content-Length: with keep-alive, extra
            # bytes from a file that GREW mid-transfer would corrupt the
            # next response's framing.
            while sent < size:
                chunk = f.read(min(1024 * 1024, size - sent))
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
                # An in-flight transfer is not "idle" — a 77MB file on weak
                # Wi-Fi must not trip the 10-min watchdog mid-download.
                self.server.touch()
        finally:
            f.close()
        if sent < size:
            # File SHRANK mid-transfer: fewer bytes than promised would hang
            # the client's read — kill the connection so it errors + retries.
            self.close_connection = True
            return
        self.server.emit("file", index=idx, path=entry["path"], bytes=sent)


_WATCHDOG_POLL_S = 15  # module-level so tests can shrink it


class PackServer(ThreadingHTTPServer):
    """Token-protected pack server; auto-stops on /complete or idle timeout."""

    daemon_threads = True
    # HTTPServer defaults this to 1, but on Windows SO_REUSEADDR lets bind()
    # SUCCEED on a port another process is actively listening on — the
    # "port busy" error path would never fire and requests would be routed
    # between two listeners. Exclusive bind → a busy port raises OSError.
    allow_reuse_address = False

    def __init__(self, manifest: dict, sources: list, port: int = DEFAULT_PORT,
                 on_event: Optional[Callable] = None,
                 apk_path: Optional[Path] = None):
        super().__init__(("0.0.0.0", port), _Handler)
        self.manifest = manifest
        self.manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        self.sources = sources
        self.token = secrets.token_urlsafe(16)
        self.apk_path = apk_path
        self._on_event = on_event
        self._last_activity = time.monotonic()
        self._stopped = threading.Event()
        self._stop_reason: Optional[str] = None
        # Lazily-baked zip for the browser bootstrap path (/v1/zip).
        self.zip_path: Optional[Path] = None
        self.zip_ready = threading.Event()
        self._zip_lock = threading.Lock()
        self._zip_started = False

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        threading.Thread(target=self.serve_forever, name="MobileSyncServer",
                         daemon=True).start()
        threading.Thread(target=self._idle_watchdog, name="MobileSyncIdle",
                         daemon=True).start()

    def request_stop(self, reason: str):
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._stop_reason = reason
        # shutdown() blocks until serve_forever exits — never call it from a
        # handler thread directly or it deadlocks the pool.
        threading.Thread(target=self._shutdown_now, daemon=True).start()

    def _shutdown_now(self):
        try:
            self.shutdown()
            self.server_close()
        except Exception:
            logger.exception("mobile_sync: server shutdown failed")
        if self.zip_path is not None:
            try:
                self.zip_path.unlink(missing_ok=True)
            except OSError:
                pass
        self.emit("stopped", reason=self._stop_reason)

    # -- browser bootstrap (USB-free install) ------------------------------

    def ensure_zip_build(self):
        """Start baking the pack zip once, in the background (idempotent)."""
        with self._zip_lock:
            if self._zip_started:
                return
            self._zip_started = True
        threading.Thread(target=self._build_zip, name="MobileSyncZipBake",
                         daemon=True).start()

    def _build_zip(self):
        try:
            dest = Path(tempfile.gettempdir()) / f"lsb_soundpack_{self.port}.zip"
            export_zip(self.manifest, self.sources, dest)
            self.zip_path = dest
            self.emit("zip_baked", bytes=dest.stat().st_size)
        except Exception:
            logger.exception("mobile_sync: zip bake failed")
        finally:
            self.zip_ready.set()

    def landing_html(self) -> str:
        """Phone-browser page: install the app + download the pack, no USB."""
        t = self.manifest["totals"]
        mb = t["bytes"] // (1024 * 1024)
        apk_ok = self.apk_path is not None and self.apk_path.is_file()
        apk_button = (
            f'<a class="btn" href="/v1/apk?token={self.token}">⬇ Download the app (APK)</a>'
            '<p class="hint">When the download finishes, open it and allow the '
            'install (Samsung asks once per browser). Only needed the first time '
            'or after an app update.</p>'
            if apk_ok else
            '<p class="hint">⚠ App file not found on the PC — build it first '
            '(mobile/android), then reopen this page.</p>'
        )
        return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LocalSoundBoard → Phone</title><style>
body{{background:#1E1F22;color:#F2F3F5;font-family:sans-serif;margin:0;padding:24px}}
.card{{background:#313338;border-radius:14px;padding:18px;margin-bottom:16px}}
h1{{font-size:20px;margin:0 0 18px}} h2{{font-size:15px;margin:0 0 8px}}
.btn{{display:block;background:#5865F2;color:#fff;text-decoration:none;text-align:center;
padding:16px;border-radius:12px;font-size:17px;font-weight:bold;margin:10px 0}}
.hint{{color:#B5BAC1;font-size:13px;margin:6px 0 0}}
.muted{{color:#80848E;font-size:12px}}</style></head><body>
<h1>🎵 LocalSoundBoard → Phone</h1>
<div class="card"><h2>Step 1 — the app</h2>{apk_button}</div>
<div class="card"><h2>Step 2 — your sounds</h2>
<a class="btn" href="/v1/zip?token={self.token}">⬇ Download sound pack ({mb} MB)</a>
<p class="hint">The PC packs it in the background — if the tap seems to hang for
up to a minute, it's still preparing; the download then starts with a progress
bar.</p></div>
<div class="card"><h2>Step 3 — import</h2>
<p class="hint">Open <b>LSB Mobile</b> → ⋮ → <b>Sync / Import</b> →
<b>Import SoundPack (.zip)</b> → pick the file from Downloads.</p></div>
<p class="muted">{t["files"]} files · exported {self.manifest.get("exported_at", "")}
· keep the PC dialog open while downloading</p>
</body></html>"""

    def _idle_watchdog(self):
        while not self._stopped.wait(_WATCHDOG_POLL_S):
            if time.monotonic() - self._last_activity > IDLE_TIMEOUT_S:
                self.request_stop("timeout")
                return

    # -- misc --------------------------------------------------------------
    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def landing_url(self, ip: str) -> str:
        """QR target: the browser landing page (camera-app scan → install +
        download, no USB). The Phase-2 in-app scanner accepts this same URL
        and derives the manifest endpoint from it."""
        return f"http://{ip}:{self.port}/?token={self.token}"

    def manifest_url(self, ip: str) -> str:
        return f"http://{ip}:{self.port}/v1/manifest?token={self.token}"

    def touch(self):
        self._last_activity = time.monotonic()

    def emit(self, event: str, **kw):
        if self._on_event:
            try:
                self._on_event(event, **kw)
            except Exception:
                logger.exception("mobile_sync: on_event(%s) failed", event)


# =============================================================================
# 📱 Send to Phone dialog
# =============================================================================

class MobileSyncDialog:
    """Non-modal dialog: builds the pack on a worker, serves it, shows a QR.

    ``app`` is the SoundboardGUI instance (duck-typed: uses ``root``,
    ``_scaffold_dialog``). The caller flushes the config to disk first so the
    builder reads a fresh snapshot — this class never touches live GUI state.
    """

    def __init__(self, app):
        import customtkinter as ctk
        from .constants import COLORS, FONTS, UI

        self.app = app
        self.ctk = ctk
        self.COLORS = COLORS
        self.server: Optional[PackServer] = None
        self.manifest = None
        self.sources = None
        # Resolved on the build worker — getaddrinfo can stall for seconds
        # on VPN/misconfigured-DNS setups and must not freeze the Tk thread.
        self._ips: list[str] = []
        self._served_files = 0
        self._served_bytes = 0
        self._alive = True

        self.dialog, body, footer, _ = app._scaffold_dialog(
            "📱 Send to Phone", 520, 660,
            subtitle="Move the sound library to the LSB Mobile app",
            modal=False, on_close=self._close,
        )
        # The scaffold wires ✕/Escape to on_close but not the OS titlebar X —
        # hook it too, or closing that way would leave the server running.
        self.dialog.protocol("WM_DELETE_WINDOW", self._close)
        # DPI factor: the QR is rasterised at device px (see _refresh_qr).
        try:
            self._scale = float(ctk.ScalingTracker.get_window_scaling(self.dialog))
        except Exception:
            self._scale = 1.0
        if not (0.4 <= self._scale <= 8.0):
            self._scale = 1.0
        self._font = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"])
        self._font_bold = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"],
                                      weight="bold")

        # Status card ------------------------------------------------------
        card = ctk.CTkFrame(body, fg_color=COLORS["bg_medium"], corner_radius=8)
        card.pack(fill="x", padx=0, pady=(0, 10))
        self.status_label = ctk.CTkLabel(card, text="Preparing library…",
                                         font=self._font_bold, anchor="w")
        self.status_label.pack(fill="x", padx=12, pady=(10, 2))
        self.detail_label = ctk.CTkLabel(card, text="", font=self._font, anchor="w",
                                         text_color=COLORS["text_muted"])
        self.detail_label.pack(fill="x", padx=12, pady=(0, 8))
        self.progress = ctk.CTkProgressBar(card, height=8)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=12, pady=(0, 12))

        # QR card ----------------------------------------------------------
        self.qr_card = ctk.CTkFrame(body, fg_color=COLORS["bg_medium"], corner_radius=8)
        self.qr_card.pack(fill="x", padx=0, pady=(0, 10))
        ctk.CTkLabel(self.qr_card,
                     text="Scan with the phone's CAMERA app — a page opens with\n"
                          "the app install + sound-pack download (no USB needed)",
                     font=self._font_bold, justify="left").pack(padx=12, pady=(10, 4))
        self.qr_label = ctk.CTkLabel(self.qr_card, text="…", font=self._font)
        self.qr_label.pack(padx=12, pady=4)
        self.url_label = ctk.CTkLabel(self.qr_card, text="", font=self._font,
                                      text_color=COLORS["text_muted"], wraplength=440)
        self.url_label.pack(padx=12, pady=(0, 4))
        row = ctk.CTkFrame(self.qr_card, fg_color="transparent")
        row.pack(pady=(0, 10))
        self._ip_row = row
        self.ip_var = None  # OptionMenu built in _on_pack_ready if >1 IP
        self.test_btn = ctk.CTkButton(
            row, text="🌐 Open in browser", command=self._self_test, width=140,
            font=self._font, fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"], state="disabled")
        self.test_btn.pack(side="left", padx=4)

        # Footer -----------------------------------------------------------
        # Footer: Export (left) and Close (right), both secondary — the phone
        # drives the transfer, there is no primary action here.
        self.zip_btn = ctk.CTkButton(
            footer, text="💾 Export .zip instead", command=self._export_zip,
            font=self._font, fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"], state="disabled", width=170,
            height=UI["footer_button_height"], corner_radius=UI["button_corner_radius"])
        self.zip_btn.pack(side="left", padx=(12, 4), pady=10)
        ctk.CTkButton(footer, text="Close", command=self._close, width=90,
                      font=self._font, fg_color=COLORS["bg_light"],
                      hover_color=COLORS["bg_lighter"],
                      height=UI["footer_button_height"],
                      corner_radius=UI["button_corner_radius"]).pack(
            side="right", padx=12, pady=10)
        note = ("First time: allow the Windows Firewall prompt on Private networks. "
                "Phone must be on the same Wi-Fi (not a guest network).")
        ctk.CTkLabel(body, text=note, font=self._font, wraplength=460,
                     text_color=COLORS["text_muted"], justify="left").pack(
            fill="x", padx=8, pady=(0, 6))

        threading.Thread(target=self._build_worker, name="MobileSyncBuild",
                         daemon=True).start()

    # -- worker → UI marshalling ------------------------------------------
    def _ui(self, fn, *a):
        if not self._alive:
            return

        def run():
            # Re-check at EXECUTION time: an after() callback queued just
            # before _close() still fires after the widgets are destroyed.
            if self._alive:
                fn(*a)

        try:
            self.dialog.after(0, run)
        except Exception:
            pass

    def is_active(self) -> bool:
        """True while this dialog is still useful (building or serving).

        False once the server stopped (complete / idle timeout / port
        failure) — the 📱 button then discards this dialog and opens a
        fresh one instead of lifting a dead window.
        """
        if not self._alive:
            return False
        return self.server is None or not self.server.stopped

    def _build_worker(self):
        from .constants import CONFIG_FILE
        self._ips = get_lan_ips()
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            logger.exception("mobile_sync: config read failed")
            self._ui(self._set_status, "❌ Could not read the config file.", "", 0)
            return

        root = Path.cwd()

        def on_progress(stage, done, total, detail):
            if done == total or done % 25 == 0:
                self._ui(self._set_status, f"Hashing library… {done}/{total}",
                         detail, done / max(total, 1))

        try:
            manifest, sources = build_pack(config, root, progress=on_progress)
            attach_app_info(manifest, root)  # phone offers in-app updates from this
        except Exception:
            logger.exception("mobile_sync: pack build failed")
            self._ui(self._set_status, "❌ Pack build failed — see debug.log.", "", 0)
            return
        self.manifest, self.sources = manifest, sources
        self._ui(self._on_pack_ready)

    def _on_pack_ready(self):
        if not self._alive:
            return  # closed mid-build — don't start a server for a dead dialog
        t = self.manifest["totals"]
        skipped = len(self.manifest["skipped"])
        mb = t["bytes"] / (1024 * 1024)
        detail = f"{t['files']} files · {mb:.0f} MB" + (
            f" · {skipped} skipped" if skipped else "")
        apk = find_apk(Path.cwd())
        try:
            self.server = PackServer(self.manifest, self.sources,
                                     on_event=self._on_server_event,
                                     apk_path=apk)
        except OSError:
            # Default port taken (another app, or a stale instance) — any
            # port works, the QR encodes the real one. Fall back to ephemeral.
            try:
                self.server = PackServer(self.manifest, self.sources, port=0,
                                         on_event=self._on_server_event,
                                         apk_path=apk)
            except OSError:
                logger.exception("mobile_sync: server bind failed")
                self._set_status("❌ Could not open a network port — "
                                 "press 📱 again to retry.", detail, 0)
                self.zip_btn.configure(state="normal")
                return
        self.server.start()
        if len(self._ips) > 1 and self.ip_var is None:
            self.ip_var = self.ctk.StringVar(value=self._ips[0])
            self.ctk.CTkOptionMenu(
                self._ip_row, values=self._ips, variable=self.ip_var,
                command=lambda _v: self._refresh_qr(), width=150,
                font=self._font).pack(side="left", padx=4,
                                      before=self.test_btn)
        self._set_status("Ready — scan the QR with your phone.", detail, 0)
        self.zip_btn.configure(state="normal")
        self.test_btn.configure(state="normal")
        self._refresh_qr()

    def _set_status(self, text, detail, frac):
        self.status_label.configure(text=text)
        self.detail_label.configure(text=detail)
        self.progress.set(frac)

    def _current_ip(self) -> str:
        if self.ip_var is not None:
            return self.ip_var.get()
        return self._ips[0] if self._ips else "127.0.0.1"

    def _refresh_qr(self):
        if not self.server:
            return
        url = self.server.landing_url(self._current_ip())
        self.url_label.configure(text=url)
        # Rasterise at DEVICE px and hand CTkImage the LOGICAL size: the raster
        # already equals round(300 * scaling), so CTk shows it unresampled.
        pil = make_qr_image(url, target_px=max(1, round(300 * self._scale)))
        if pil is None:
            self.qr_label.configure(
                text="qrcode package missing —\ntype the URL below into the "
                     "phone app instead\n(pip install qrcode)", image=None)
            return
        img = self.ctk.CTkImage(light_image=pil, dark_image=pil,
                                size=(300, 300))
        self.qr_label.configure(image=img, text="")
        self.qr_label._qr_image = img  # keep a ref; Tk drops unreferenced images

    def _self_test(self):
        if self.server:
            import webbrowser
            webbrowser.open(self.server.landing_url(self._current_ip()))

    # -- server events (called from server threads!) -----------------------
    def _on_server_event(self, event, **kw):
        self._ui(self._apply_server_event, event, kw)

    def _apply_server_event(self, event, kw):
        total = self.manifest["totals"]
        if event == "request" and kw.get("what") == "landing":
            self._set_status("📶 Phone opened the download page — packing zip…",
                             "", 0)
        elif event == "zip_baked":
            self._set_status("Pack ready — waiting for the phone to download.",
                             f"{kw.get('bytes', 0) / (1024 * 1024):.0f} MB zip", 0)
        elif event == "download":
            what = "app (APK)" if kw.get("what") == "apk" else "sound pack"
            self._set_status(f"✅ Phone downloaded the {what}.",
                             "Follow the steps on the phone's page.", 1.0)
        elif event == "request":
            self._set_status("📶 Phone connected — sending…", "", 0)
        elif event == "file":
            self._served_files += 1
            self._served_bytes += kw.get("bytes", 0)
            frac = self._served_files / max(total["files"], 1)
            self._set_status(
                f"Sending… {self._served_files}/{total['files']} files",
                f"{self._served_bytes / (1024 * 1024):.0f} MB · {kw.get('path', '')}",
                min(frac, 1.0))
        elif event == "complete":
            self._set_status("✅ Phone finished syncing!",
                             f"{self._served_files} files sent this session", 1.0)
            self.test_btn.configure(state="disabled")
        elif event == "stopped" and kw.get("reason") == "timeout":
            self._set_status("Server stopped (idle 10 min). "
                             "Press 📱 again to share more.", "", 0)
            self.test_btn.configure(state="disabled")

    # -- zip export --------------------------------------------------------
    def _export_zip(self):
        from tkinter import filedialog
        dest = filedialog.asksaveasfilename(
            parent=self.dialog, defaultextension=".zip",
            initialfile=f"soundpack_{datetime.now():%Y%m%d}.zip",
            filetypes=[("SoundPack zip", "*.zip")])
        if not dest:
            return
        self.zip_btn.configure(state="disabled")

        def worker():
            def on_progress(stage, done, total, detail):
                if done == total or done % 10 == 0:
                    self._ui(self._set_status, f"Writing zip… {done}/{total}",
                             detail, done / max(total, 1))
            try:
                export_zip(self.manifest, self.sources, Path(dest),
                           progress=on_progress)
                self._ui(self._set_status, "✅ Zip exported.", dest, 1.0)
            except Exception:
                logger.exception("mobile_sync: zip export failed")
                self._ui(self._set_status, "❌ Zip export failed — see debug.log.",
                         "", 0)
            self._ui(lambda: self.zip_btn.configure(state="normal"))

        threading.Thread(target=worker, name="MobileSyncZip", daemon=True).start()

    # -- teardown ----------------------------------------------------------
    def _close(self):
        self._alive = False
        if self.server:
            self.server.request_stop("dialog-closed")
        if getattr(self.app, "_mobile_sync_dialog", None) is self:
            self.app._mobile_sync_dialog = None
        try:
            self.dialog.destroy()
        except Exception:
            pass
