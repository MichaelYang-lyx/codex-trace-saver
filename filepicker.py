"""Scan a Codex rollout JSONL for referenced file paths + apply safety filters.

Codex writes one JSON event per line under
``~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-<uuid>.jsonl``:

  {"type":"message","role":"user","content":[{"type":"input_text","text":"..."}]}
  {"type":"function_call","name":"shell","arguments":"{\"command\":[...]}","call_id":"..."}
  {"type":"function_call_output","call_id":"...","output":"..."}
  {"type":"reasoning","summary":[...]}

We collect file paths from both function_call arguments AND their
matched function_call_output rows — many searches only surface the
actual hit list in the output. Relative paths are resolved against
uploader.codex_cwd() (from /proc), matching how the hermes plugin does
it for search_files.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

try:
    from . import uploader  # type: ignore[attr-defined]
except ImportError:  # standalone / CLI
    import uploader  # type: ignore[no-redef]


# --------------------------------------------------------------------------- #
# Filters (mirrors the hermes plugin so both save the same-safety set)
# --------------------------------------------------------------------------- #
DEFAULT_MAX_SIZE_BYTES = 50 * 1024 * 1024

_SENSITIVE_NAME_RE = re.compile(
    r"(?:^|[/.])(?:"
    r"\.env(\..+)?|"
    r"\.netrc|"
    r"id_rsa|id_ed25519|id_ecdsa|"
    r".+\.pem|.+\.key|"
    r".+(secret|token|credential|password)s?.*"
    r")$",
    re.IGNORECASE,
)

_EXCLUDED_DIR_NAMES = {
    ".hermes", ".codex", ".claude", ".cache", ".ssh", ".gnupg",
    "node_modules", ".git", "__pycache__",
    ".venv", "venv", ".mypy_cache", ".pytest_cache", ".tox",
}


def _is_sensitive_name(path: Path) -> bool:
    return bool(_SENSITIVE_NAME_RE.search(path.name))


def _in_excluded_dir(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError):
        resolved = path
    return any(part in _EXCLUDED_DIR_NAMES for part in resolved.parts)


def _too_big(path: Path, max_bytes: int) -> bool:
    try:
        return path.stat().st_size > max_bytes
    except OSError:
        return False


def filter_paths(paths: Iterable[Path],
                 max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES,
                 ) -> Tuple[List[Path], List[Tuple[Path, str]]]:
    kept: List[Path] = []
    rejected: List[Tuple[Path, str]] = []
    seen: Set[Path] = set()
    for raw in paths:
        try:
            p = Path(raw).expanduser()
        except Exception:
            rejected.append((Path(str(raw)), "bad path"))
            continue
        try:
            resolved = p.resolve()
        except (OSError, RuntimeError):
            resolved = p
        if resolved in seen:
            continue
        seen.add(resolved)
        if not p.exists() or not p.is_file():
            rejected.append((p, "missing or not a regular file"))
            continue
        if _in_excluded_dir(p):
            rejected.append((p, "under an excluded directory"))
            continue
        if _is_sensitive_name(p):
            rejected.append((p, "sensitive filename"))
            continue
        if _too_big(p, max_size_bytes):
            rejected.append((p, f"> {max_size_bytes // (1024*1024)} MB"))
            continue
        kept.append(p)
    return kept, rejected


# --------------------------------------------------------------------------- #
# Codex-specific path extraction
# --------------------------------------------------------------------------- #
# Tool names Codex uses that carry filesystem paths.
_PATH_ARG_TOOLS = {
    "read_file", "write_file", "view_file", "edit_file",
    "apply_patch",  # Codex's canonical file-edit tool
}
_SHELL_TOOLS = {"shell", "exec", "bash", "container.exec"}
_SEARCH_TOOLS = {"search_files", "grep", "glob", "find_files", "list_files"}

# Absolute or home-relative path token in a shell command.
_ABS_OR_HOME_PATH_RE = re.compile(r"(?<![\w])(/[^\s'\"`;|&<>()]+|~/[^\s'\"`;|&<>()]+)")
# Simple ./foo.ext or foo.ext token — for scanning result output.
_REL_PATH_RE = re.compile(r"(?:\./|(?<![\w./]))([\w.\-一-鿿]+\.[A-Za-z0-9]{1,8})")


def _load_shell_command(args_obj) -> str:
    """Codex passes `shell` command as either a string or a list."""
    if isinstance(args_obj, dict):
        cmd = args_obj.get("command") or args_obj.get("cmd")
    else:
        cmd = args_obj
    if isinstance(cmd, list):
        try:
            return " ".join(shlex.quote(str(x)) for x in cmd)
        except Exception:
            return " ".join(str(x) for x in cmd)
    if isinstance(cmd, str):
        return cmd
    return ""


def _paths_from_function_call(name: str, args_raw) -> Tuple[List[str], Optional[str]]:
    """Extract paths + base-dir hint from a Codex function_call event."""
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
    except (TypeError, ValueError):
        args = {}
    if not isinstance(args, dict):
        args = {}

    out: List[str] = []
    base: Optional[str] = None

    if name in _PATH_ARG_TOOLS:
        for key in ("path", "file_path", "target_file", "filename"):
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        # apply_patch sometimes carries a list of edits with paths
        for key in ("edits", "changes", "patches"):
            v = args.get(key)
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        for k2 in ("path", "file_path", "filename"):
                            s = item.get(k2)
                            if isinstance(s, str) and s.strip():
                                out.append(s.strip())
                                break
    elif name in _SHELL_TOOLS:
        cwd = args.get("cwd") or args.get("workdir")
        if isinstance(cwd, str) and cwd.strip():
            base = cwd.strip()
        cmd = _load_shell_command(args)
        if cmd:
            try:
                for tok in shlex.split(cmd, posix=True):
                    if tok.startswith(("/", "~")):
                        out.append(tok)
            except ValueError:
                pass
            for m in _ABS_OR_HOME_PATH_RE.findall(cmd):
                out.append(m)
    elif name in _SEARCH_TOOLS:
        for key in ("cwd", "root", "dir", "directory", "path", "target"):
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                s = v.strip()
                if s.startswith(("/", "~", "./", "../")) or "/" in s:
                    base = s
                    break
    return out, base


def _paths_from_result_output(content, base: Optional[str]) -> List[str]:
    """Pull paths from a Codex function_call_output text."""
    if not content:
        return []
    text = content if isinstance(content, str) else str(content)
    out: List[str] = []

    # JSON envelope (search-style tools)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("files", "paths", "results", "matches", "found"):
                v = data.get(key)
                if isinstance(v, list):
                    for item in v:
                        if isinstance(item, str) and item.strip():
                            out.append(item.strip())
                        elif isinstance(item, dict):
                            for k2 in ("path", "file", "name"):
                                s = item.get(k2)
                                if isinstance(s, str) and s.strip():
                                    out.append(s.strip())
                                    break
    except (TypeError, ValueError):
        pass

    # Plain-text fallback — one path per line or ./relative tokens
    if not out:
        for line in text.splitlines():
            ln = line.strip()
            if not ln:
                continue
            if ln.startswith(("/", "~")):
                out.append(ln)
            elif ln.startswith("./") or _REL_PATH_RE.search(ln):
                out.append(ln)

    # Resolve relatives against base dir; else codex process cwd
    from pathlib import Path as _P
    base_p = _P(base).expanduser() if base else None
    if base_p is None:
        try:
            base_p = uploader.codex_cwd()
        except Exception:
            base_p = None

    resolved: List[str] = []
    for p in out:
        pp = _P(p)
        if pp.is_absolute() or p.startswith("~"):
            resolved.append(p)
        elif base_p is not None:
            rel = p[2:] if p.startswith("./") else p
            resolved.append(str(base_p / rel))
        else:
            resolved.append(p)
    return resolved


def _extract_function_call(evt: dict) -> Optional[Tuple[str, str, str]]:
    """Return (name, arguments, call_id) if *evt* is a function_call, else None.

    Handles multiple shapes we've seen in Codex/Responses-API rollouts:
      Codex Responses-style:
        {"type":"function_call","name":"shell","arguments":"...","call_id":"..."}
      Nested (chat-completions-style leftover):
        {"type":"message","message":{"tool_calls":[{"function":{"name":..,
          "arguments":..},"id":"..."}]}}
    """
    t = evt.get("type")
    if t == "function_call":
        return (evt.get("name") or "",
                evt.get("arguments") or "{}",
                evt.get("call_id") or evt.get("id") or "")
    return None


def scan_rollout(session_path: Optional[Path] = None) -> List[str]:
    """Return candidate file paths mentioned in the rollout JSONL."""
    if session_path is None:
        session_path = uploader.current_session_file()
    if session_path is None:
        raise FileNotFoundError(
            f"No Codex rollout found under {uploader.sessions_dir()}"
        )

    seen: Set[str] = set()
    ordered: List[str] = []
    # call_id -> base dir hint carried over from the function_call so we can
    # resolve relatives in the matching function_call_output later.
    call_bases: dict = {}

    def _remember(paths):
        for p in paths:
            if p and p not in seen:
                seen.add(p)
                ordered.append(p)

    with open(session_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(evt, dict):
                continue

            fc = _extract_function_call(evt)
            if fc is not None:
                name, args_raw, call_id = fc
                paths, base = _paths_from_function_call(name, args_raw)
                _remember(paths)
                if call_id and base:
                    call_bases[call_id] = base
                continue

            if evt.get("type") == "function_call_output":
                base = call_bases.get(evt.get("call_id") or "")
                _remember(_paths_from_result_output(evt.get("output"), base))
                continue

            # Also handle chat-completions leftover shape (message with tool_calls)
            msg = evt.get("message") or evt
            tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else None
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                    name = fn.get("name", "")
                    args_raw = fn.get("arguments") or "{}"
                    call_id = tc.get("id") or tc.get("call_id") or ""
                    paths, base = _paths_from_function_call(name, args_raw)
                    _remember(paths)
                    if call_id and base:
                        call_bases[call_id] = base

    return ordered


# --------------------------------------------------------------------------- #
# Preview text
# --------------------------------------------------------------------------- #
def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def format_preview(kept: Sequence[Path], rejected: Sequence[Tuple[Path, str]],
                   *, session_path: Optional[Path] = None) -> str:
    lines: List[str] = []
    if session_path is not None:
        lines.append(f"rollout: {Path(session_path).name}")
    total = sum(_safe_size(p) for p in kept)
    lines.append(f"attach files: {len(kept)} file(s), ~{total / 1024:.1f} KB")
    for p in kept:
        lines.append(f"  ✓ {p}  ({_safe_size(p)} B)")
    if rejected:
        lines.append(f"skipped: {len(rejected)} file(s)")
        for p, why in rejected[:20]:
            lines.append(f"  ✗ {p}  — {why}")
        if len(rejected) > 20:
            lines.append(f"  ... and {len(rejected) - 20} more")
    return "\n".join(lines)
