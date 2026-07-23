"""codex-trace-saver — package Codex session rollouts and upload to the leaderboard.

Codex stores each conversation as a JSONL "rollout" file under
``~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-<uuid>.jsonl``. Each line is
one event (user message, agent message, function_call, function_call_output,
reasoning, etc.). We treat the newest rollout as "the current session" —
Codex flushes each event as it happens, so mtime is a reliable signal
(unlike Hermes, which keeps live conversations in state.db and only exports
JSON on close).

Upload target and zip layout match the Hermes version, so the same leaderboard
server serves both. Each upload scores +1.
"""

from __future__ import annotations

import getpass
import glob
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_URL = "http://10.9.66.12:8848"
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #
def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def sessions_dir() -> Path:
    return codex_home() / "sessions"


def leaderboard_url() -> str:
    return os.environ.get("TRACE_LEADERBOARD_URL", DEFAULT_URL).rstrip("/")


def default_name() -> str:
    name = os.environ.get("TRACE_LEADERBOARD_NAME", "").strip()
    if name:
        return name
    try:
        return getpass.getuser() or "codex"
    except Exception:
        return "codex"


def default_save_dir() -> Path:
    raw = os.environ.get("TRACE_SAVE_DIR", "").strip()
    return Path(raw).expanduser() if raw else (Path.home() / "codex-traces")


def _safe(part: str, fallback: str = "trace") -> str:
    part = _SAFE_RE.sub("_", (part or "").strip()).strip("._")
    return part or fallback


# --------------------------------------------------------------------------- #
# Rollout discovery
# --------------------------------------------------------------------------- #
def list_rollouts() -> List[Path]:
    """All rollout-*.jsonl files anywhere under ``sessions/``, newest first."""
    root = sessions_dir()
    if not root.is_dir():
        return []
    files = [Path(p) for p in glob.glob(str(root / "**" / "rollout-*.jsonl"),
                                        recursive=True)]
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return files


def current_session_file() -> Optional[Path]:
    """Return the rollout for the CURRENT / most-recent Codex session.

    Codex flushes events to the JSONL file as they happen, so the newest
    rollout by mtime is the live one (or was, moments ago). If a session
    id / rollout id is exposed via env (``CODEX_SESSION_ID``,
    ``CODEX_ROLLOUT_ID``, ``CODEX_SESSION_FILE``), prefer that.
    """
    # 1) Explicit file path (some Codex integrations set this)
    explicit = os.environ.get("CODEX_SESSION_FILE", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p

    # 2) Session/rollout id -> filename substring match
    for var in ("CODEX_SESSION_ID", "CODEX_ROLLOUT_ID", "SESSION_ID"):
        sid = os.environ.get(var, "").strip()
        if sid:
            for f in list_rollouts():
                if sid in f.name:
                    return f

    # 3) Fall back to newest by mtime (Codex flushes live)
    files = list_rollouts()
    return files[0] if files else None


def resolve_sessions(session: str) -> Tuple[List[Path], str]:
    """Resolve the ``session`` selector to a list of rollouts + zip label.

    ``session`` accepts:
      * ``"latest"`` / empty / ``"current"`` — current or newest rollout
      * ``"all"`` — every rollout on disk
      * a session id / filename substring — matched against filenames
    """
    session = (session or "latest").strip()
    all_files = list_rollouts()
    if not all_files:
        raise FileNotFoundError(
            f"No Codex rollouts found under {sessions_dir()}"
        )

    if session in ("all", "*"):
        return all_files, "all"

    if session in ("latest", "", "last", "current"):
        cur = current_session_file()
        if cur is not None:
            return [cur], _safe(cur.stem, "current")
        return [all_files[0]], _safe(all_files[0].stem, "latest")

    matches = [p for p in all_files if session in p.name]
    if not matches:
        raise FileNotFoundError(
            f"No rollout matching '{session}' under {sessions_dir()} "
            f"({len(all_files)} rollouts available; use 'latest' or 'all')"
        )
    return matches, _safe(session)


# --------------------------------------------------------------------------- #
# Codex-process cwd (used by the scanner to resolve relative paths)
# --------------------------------------------------------------------------- #
def codex_cwd() -> Optional[Path]:
    """Return the cwd of a live ``codex`` binary process (best effort).

    We match processes whose argv contains a token with basename == ``codex``.
    That matches ``/usr/local/bin/codex``, npm-installed shims, etc., but
    excludes unrelated python scripts. Deepest cwd wins.
    """
    import glob as _glob
    my_pid = os.getpid()
    candidates: List[Path] = []
    try:
        for cmd_path in _glob.glob("/proc/*/cmdline"):
            try:
                pid = int(cmd_path.split("/")[2])
            except (IndexError, ValueError):
                continue
            if pid == my_pid:
                continue
            try:
                with open(cmd_path, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            argv = [a for a in raw.split(b"\x00") if a]
            if not argv:
                continue
            if not any(os.path.basename(a.decode(errors="replace")) == "codex"
                       for a in argv):
                continue
            pid_dir = os.path.dirname(cmd_path)
            try:
                cwd = os.readlink(os.path.join(pid_dir, "cwd"))
            except OSError:
                continue
            p = Path(cwd)
            if p.is_dir():
                candidates.append(p)
    except Exception:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda p: (len(p.parts), str(p)), reverse=True)
    return candidates[0]


# --------------------------------------------------------------------------- #
# Zip packaging
# --------------------------------------------------------------------------- #
def build_combined_zip(session_files: List[Path], extra_files: List[Path],
                       name: str, label: str = "latest", note: str = "",
                       now: Optional[datetime] = None,
                       out_dir: Optional[Path] = None) -> Path:
    """Zip Codex rollout(s) + extra work files into one archive.

    Layout::

        manifest.json
        sessions/rollout-<uuid>.jsonl
        files/<basename>
    """
    now = now or datetime.now()
    ts = now.strftime("%Y%m%d_%H%M%S")
    zip_name = f"codex_trace_{_safe(name)}_{_safe(label)}_{ts}.zip"

    if out_dir is None:
        target_dir = Path(tempfile.mkdtemp(prefix="codex-trace-saver-"))
    else:
        target_dir = Path(out_dir).expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)
    zip_path = target_dir / zip_name

    used: dict = {}
    file_entries: List[dict] = []
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in session_files:
            f = Path(f)
            if f.exists():
                zf.write(f, arcname=f"sessions/{f.name}")
        for f in extra_files:
            f = Path(f)
            if not f.exists() or not f.is_file():
                continue
            base = f.name or "unnamed"
            if base in used:
                used[base] += 1
                stem, dot, ext = base.rpartition(".")
                base = (f"{stem}__{used[base]}.{ext}" if dot
                        else f"{base}__{used[base]}")
            else:
                used[base] = 0
            arc = f"files/{base}"
            zf.write(f, arcname=arc)
            file_entries.append({"arcname": arc, "source": str(f),
                                 "size": f.stat().st_size})

        manifest = {
            "kind": "codex-trace+files",
            "board_name": name,
            "selector": label,
            "created_at": now.astimezone().isoformat(),
            "note": note or "",
            "session_count": len([f for f in session_files if Path(f).exists()]),
            "sessions": [Path(f).name for f in session_files if Path(f).exists()],
            "file_count": len(file_entries),
            "files": file_entries,
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return zip_path


# --------------------------------------------------------------------------- #
# HTTP: leaderboard login + upload (requests preferred, urllib fallback)
# --------------------------------------------------------------------------- #
def _healthz(base_url: str, timeout: float = 5.0) -> None:
    url = f"{base_url}/healthz"
    try:
        import requests  # type: ignore
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f"leaderboard healthz returned {r.status_code}")
        return
    except ImportError:
        pass
    except Exception as exc:
        raise ConnectionError(f"Trace Leaderboard unreachable at {url}: {exc}") from exc

    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            if resp.status != 200:
                raise RuntimeError(f"leaderboard healthz returned {resp.status}")
    except Exception as exc:
        raise ConnectionError(f"Trace Leaderboard unreachable at {url}: {exc}") from exc


def _upload_requests(base_url: str, name: str, zip_path: Path, timeout: float) -> int:
    import requests  # type: ignore
    s = requests.Session()
    r = s.post(f"{base_url}/login", data={"name": name}, timeout=timeout,
               allow_redirects=False)
    if r.status_code not in (200, 303):
        raise RuntimeError(f"login failed: HTTP {r.status_code} {r.text[:200]}")
    with zip_path.open("rb") as fh:
        r = s.post(
            f"{base_url}/upload",
            files=[("files", (zip_path.name, fh, "application/zip"))],
            timeout=timeout, allow_redirects=False,
        )
    if r.status_code not in (200, 303):
        raise RuntimeError(f"upload failed: HTTP {r.status_code} {r.text[:200]}")
    return r.status_code


def _upload_urllib(base_url: str, name: str, zip_path: Path, timeout: float) -> int:
    import http.cookiejar
    import urllib.parse
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # noqa: D401,ANN001
            return None

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar), _NoRedirect
    )

    login_body = urllib.parse.urlencode({"name": name}).encode()
    login_req = urllib.request.Request(  # noqa: S310
        f"{base_url}/login", data=login_body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with opener.open(login_req, timeout=timeout) as resp:  # noqa: S310
            login_status = resp.status
    except urllib.error.HTTPError as exc:
        if exc.code == 303:
            login_status = 303
        else:
            raise RuntimeError(f"login failed: HTTP {exc.code} {exc.read()[:200]!r}") from exc
    if login_status not in (200, 303):
        raise RuntimeError(f"login failed: HTTP {login_status}")

    boundary = "----codex-trace-saver-boundary"
    crlf = b"\r\n"
    body = bytearray()
    body += b"--" + boundary.encode() + crlf
    body += (b'Content-Disposition: form-data; name="files"; filename="'
             + zip_path.name.encode("utf-8") + b'"' + crlf)
    body += b"Content-Type: application/zip" + crlf + crlf
    body += zip_path.read_bytes() + crlf
    body += b"--" + boundary.encode() + b"--" + crlf

    req = urllib.request.Request(  # noqa: S310
        f"{base_url}/upload", data=bytes(body), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status
    except urllib.error.HTTPError as exc:
        if exc.code == 303:
            return 303
        raise RuntimeError(f"upload failed: HTTP {exc.code} {exc.read()[:200]!r}") from exc


def upload_zip(zip_path: Path, name: str, base_url: Optional[str] = None,
               timeout: float = 300.0) -> int:
    base_url = (base_url or leaderboard_url()).rstrip("/")
    _healthz(base_url)
    try:
        import requests  # noqa: F401
        return _upload_requests(base_url, name, zip_path, timeout)
    except ImportError:
        return _upload_urllib(base_url, name, zip_path, timeout)


# --------------------------------------------------------------------------- #
# Top-level entry point (called by the CLI)
# --------------------------------------------------------------------------- #
def save_trace_bundle(session: str = "latest", extra_files: Optional[List[Path]] = None,
                      name: Optional[str] = None, note: str = "",
                      base_url: Optional[str] = None,
                      local: bool = False, out_dir: Optional[str] = None) -> dict:
    name = (name or default_name()).strip()
    if not name:
        raise ValueError("board name is empty; set TRACE_LEADERBOARD_NAME")
    extra_files = [Path(f) for f in (extra_files or [])]

    session_files, label = resolve_sessions(session)

    if local:
        target_dir = Path(out_dir).expanduser() if out_dir else default_save_dir()
        zip_path = build_combined_zip(session_files, extra_files, name=name,
                                      label=label, note=note, out_dir=target_dir)
        size = zip_path.stat().st_size
        return {
            "success": True, "mode": "local", "name": name, "selector": label,
            "sessions_saved": len(session_files), "files_saved": len(extra_files),
            "zip_bytes": size, "zip_path": str(zip_path),
            "message": (
                f"Saved rollout + {len(extra_files)} file(s) locally as '{name}' "
                f"({size / 1024:.1f} KB) -> {zip_path}"
            ),
        }

    base_url = (base_url or leaderboard_url()).rstrip("/")
    zip_path = build_combined_zip(session_files, extra_files, name=name,
                                  label=label, note=note)
    try:
        size = zip_path.stat().st_size
        status = upload_zip(zip_path, name, base_url=base_url)
    finally:
        try:
            zip_path.unlink(missing_ok=True)
            zip_path.parent.rmdir()
        except Exception:
            pass

    return {
        "success": True, "mode": "upload", "name": name, "selector": label,
        "sessions_uploaded": len(session_files), "files_uploaded": len(extra_files),
        "zip_bytes": size, "http_status": status, "leaderboard": base_url,
        "user_page": f"{base_url}/u/{name}",
        "message": (
            f"Uploaded rollout + {len(extra_files)} file(s) as '{name}' "
            f"({size / 1024:.1f} KB). See {base_url}/u/{name}"
        ),
    }
