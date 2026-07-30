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
def _sid_from_rollout(rollout: Path) -> str:
    """Extract the codex session id from a rollout filename.

    Filenames look like ``rollout-2026-07-29T18-26-53-019fad69-b302-...jsonl``.
    We take everything after the timestamp, drop the .jsonl. If the pattern
    doesn't match, fall back to the stem so we always have SOME id.
    """
    stem = rollout.stem
    if not stem.startswith("rollout-"):
        return _safe(stem, "session")
    rest = stem[len("rollout-"):]
    parts = rest.split("-", 6)  # timestamp uses first 6 dash groups
    tail = parts[-1] if len(parts) >= 7 else rest
    return _safe(tail, "session")


def _arcname_for_file(source: Path, session_cwd: Optional[str]) -> str:
    """Compute the in-zip path for a source file, preserving hierarchy.

    Layout choice: cwd-relative when possible, absolute-tree fallback when not.

    * File is inside ``session_cwd``: strip that prefix, keep the rest.
      e.g. cwd=/data/project/foo, source=/data/project/foo/sub/bar.xlsx
           → files/sub/bar.xlsx
    * File is outside cwd (or no cwd known): drop the leading '/' and keep
      the full absolute tree so the receiver can reproduce it.
      e.g. source=/tmp/other/x.txt → files/_abs/tmp/other/x.txt
    """
    try:
        src = source.resolve()
    except OSError:
        src = source
    if session_cwd:
        try:
            cwd_p = Path(session_cwd).expanduser().resolve()
            rel = src.relative_to(cwd_p)
            return f"files/{rel.as_posix()}"
        except (ValueError, OSError):
            pass
    # Fallback: absolute tree under files/_abs/
    abs_str = str(src).lstrip("/")
    return f"files/_abs/{abs_str}"


def build_combined_zip(session_bundles: List[dict],
                       name: str, label: str = "latest", note: str = "",
                       now: Optional[datetime] = None,
                       out_dir: Optional[Path] = None) -> Path:
    """Zip one-or-more Codex sessions + their attached files.

    Each ``session_bundles`` entry is a dict::

        {
            "rollout": Path(...),      # required — the rollout .jsonl
            "files":   [Path, ...],    # attached work files for this session
            "cwd":     "..." | None,   # session's cwd (for rel-path arcnames)
        }

    Layout::

        manifest.json
        sessions/<sid>/rollout.jsonl
        sessions/<sid>/files/<cwd-rel-or-_abs-path>

    Each session gets its own directory so a multi-session archive stays
    unambiguous — a file `data.txt` appearing in two sessions doesn't
    collide, and the receiver can regenerate the original layout.
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

    manifest_sessions: List[dict] = []
    total_files = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for bundle in session_bundles:
            rollout: Path = Path(bundle["rollout"])
            if not rollout.exists():
                continue
            sid = _sid_from_rollout(rollout)
            cwd = bundle.get("cwd")
            # Rollout jsonl -> sessions/<sid>/rollout.jsonl
            zf.write(rollout, arcname=f"sessions/{sid}/rollout.jsonl")

            files_manifest: List[dict] = []
            # Track arcnames within THIS session to dedup collisions
            used: dict = {}
            for f in bundle.get("files") or []:
                fp = Path(f)
                if not fp.exists() or not fp.is_file():
                    continue
                rel_arc = _arcname_for_file(fp, cwd)
                arc = f"sessions/{sid}/{rel_arc}"
                if arc in used:
                    used[arc] += 1
                    stem, dot, ext = arc.rpartition(".")
                    arc = f"{stem}__{used[arc]}.{ext}" if dot else f"{arc}__{used[arc]}"
                else:
                    used[arc] = 0
                zf.write(fp, arcname=arc)
                files_manifest.append({
                    "arcname": arc,
                    "source": str(fp),
                    "size": fp.stat().st_size,
                })
                total_files += 1

            manifest_sessions.append({
                "session_id": sid,
                "rollout_file": rollout.name,
                "cwd": cwd,
                "file_count": len(files_manifest),
                "files": files_manifest,
            })

        manifest = {
            "kind": "codex-trace+files",
            "layout_version": 2,
            "board_name": name,
            "selector": label,
            "created_at": now.astimezone().isoformat(),
            "note": note or "",
            "session_count": len(manifest_sessions),
            "file_count": total_files,
            "sessions": manifest_sessions,
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
def save_trace_bundle(session: str = "latest",
                      extra_files: Optional[List[Path]] = None,
                      files_by_session: Optional[dict] = None,
                      name: Optional[str] = None, note: str = "",
                      base_url: Optional[str] = None,
                      local: bool = False, out_dir: Optional[str] = None) -> dict:
    """Bundle codex rollout(s) + touched files into a zip; upload or save locally.

    Two ways to specify attached files:

    * ``files_by_session={sid_or_rollout_name: [Path, ...]}`` — preferred.
      Each session gets its own file list in the zip.
    * ``extra_files=[Path, ...]`` — legacy flat list. Attached to the
      first resolved session; useful for single-session use.
    """
    name = (name or default_name()).strip()
    if not name:
        raise ValueError("board name is empty; set TRACE_LEADERBOARD_NAME")

    session_files, label = resolve_sessions(session)

    # Build the per-session bundles the new build_combined_zip expects.
    bundles: List[dict] = []
    fbs = dict(files_by_session or {})
    flat_extras = [Path(f) for f in (extra_files or [])]
    for i, rollout in enumerate(session_files):
        # Match a file group by session-id, then by rollout filename, else
        # dump flat extras on the first (or only) session.
        sid = _sid_from_rollout(rollout)
        files: List[Path] = []
        for key in (sid, rollout.name, rollout.stem):
            if key in fbs:
                files = [Path(f) for f in fbs.pop(key)]
                break
        if not files and i == 0 and flat_extras:
            files = flat_extras
            flat_extras = []
        # Pull the rollout's own cwd for cwd-relative arcnames
        try:
            from . import filepicker  # type: ignore[attr-defined]
        except ImportError:
            import filepicker  # type: ignore[no-redef]
        try:
            _, cwd = filepicker.scan_rollout_with_cwd(rollout)
        except Exception:
            cwd = None
        bundles.append({"rollout": rollout, "files": files, "cwd": cwd})

    # If leftover keyed entries reference sessions not in resolve() (edge case),
    # fold them into the first bundle so nothing gets silently dropped.
    if fbs and bundles:
        for _sid, files in fbs.items():
            bundles[0]["files"].extend(Path(f) for f in files)

    total_files = sum(len(b["files"]) for b in bundles)

    if local:
        target_dir = Path(out_dir).expanduser() if out_dir else default_save_dir()
        zip_path = build_combined_zip(bundles, name=name, label=label, note=note,
                                      out_dir=target_dir)
        size = zip_path.stat().st_size
        return {
            "success": True, "mode": "local", "name": name, "selector": label,
            "sessions_saved": len(bundles), "files_saved": total_files,
            "zip_bytes": size, "zip_path": str(zip_path),
            "message": (
                f"Saved {len(bundles)} session(s) + {total_files} file(s) locally "
                f"as '{name}' ({size / 1024:.1f} KB) -> {zip_path}"
            ),
        }

    base_url = (base_url or leaderboard_url()).rstrip("/")
    zip_path = build_combined_zip(bundles, name=name, label=label, note=note)
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
        "sessions_uploaded": len(bundles), "files_uploaded": total_files,
        "zip_bytes": size, "http_status": status, "leaderboard": base_url,
        "user_page": f"{base_url}/u/{name}",
        "message": (
            f"Uploaded {len(bundles)} session(s) + {total_files} file(s) as '{name}' "
            f"({size / 1024:.1f} KB). See {base_url}/u/{name}"
        ),
    }
