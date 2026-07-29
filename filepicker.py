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
_SHELL_TOOLS = {"shell", "exec", "bash", "container.exec", "exec_command"}
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
    """Extract paths + base-dir hint from a Codex function_call event.

    ``args_raw`` may be:
      * A JSON string (classic function_call.arguments)
      * A JS-code string (codex v0.146 custom_tool_call.input, e.g.
        ``const r = await tools.exec_command({cmd:"ls /foo","workdir":"/bar"})``)
      * A dict already parsed
    """
    args: dict = {}
    args_str: str = ""
    if isinstance(args_raw, dict):
        args = args_raw
    elif isinstance(args_raw, str):
        args_str = args_raw
        try:
            parsed = json.loads(args_raw)
            if isinstance(parsed, dict):
                args = parsed
        except (TypeError, ValueError):
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
        # v0.146 custom_tool_call: args_str is JS source, not JSON. Fall
        # back to scanning the raw string for cwd+paths.
        haystack = cmd if cmd else args_str
        if haystack:
            try:
                for tok in shlex.split(haystack, posix=True):
                    if tok.startswith(("/", "~")):
                        out.append(tok)
            except ValueError:
                pass
            for m in _ABS_OR_HOME_PATH_RE.findall(haystack):
                out.append(m)
            # Also pull an embedded workdir/cwd from a JS-code style input
            # like ``exec_command({cmd:"...","workdir":"/foo"})``.
            if base is None:
                m = re.search(r'"?(workdir|cwd)"?\s*:\s*"([^"]+)"', haystack)
                if m:
                    base = m.group(2)
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


def _unwrap_event(evt: dict) -> dict:
    """Peel off codex v0.146's ``{timestamp, type, payload}`` envelope.

    Real codex v0.146 writes each rollout line as::

        {"timestamp":"...","type":"response_item","payload":{"type":"function_call",...}}
        {"timestamp":"...","type":"event_msg","payload":{"type":"task_started",...}}
        {"timestamp":"...","type":"session_meta","payload":{"cwd":"...","cli_version":"..."}}
        {"timestamp":"...","type":"turn_context","payload":{"cwd":"...","model":"..."}}

    Older / flat writers just put ``{type, name, arguments, ...}`` at the top
    level. Return the inner dict we should inspect for tool-call fields.
    """
    payload = evt.get("payload")
    if isinstance(payload, dict):
        return payload
    return evt


def _extract_function_call(evt: dict) -> Optional[Tuple[str, str, str]]:
    """Return (name, arguments, call_id) if *evt* is a function_call, else None.

    Supports:

    * codex v0.146 custom_tool_call: ``{type:'response_item', payload:{type:'custom_tool_call',name:'exec',input:'<JS code>',call_id}}``
    * codex v0.146 wrapped: ``{type:'response_item', payload:{type:'function_call',name,arguments,call_id}}``
    * flat responses: ``{type:'function_call', name, arguments, call_id}``
    * chat-completions leftover: ``{message:{tool_calls:[{function:{name,arguments},id}]}}``
    """
    inner = _unwrap_event(evt)
    inner_type = inner.get("type")
    if inner_type == "custom_tool_call":
        # Codex v0.146 stores the tool argument as a JS-code string in `input`,
        # not JSON in `arguments`. We pass the raw string through — the shell
        # branch in _paths_from_function_call() scans it with a path regex
        # that doesn't require JSON.
        return (inner.get("name") or "",
                inner.get("input") or "",
                inner.get("call_id") or inner.get("id") or "")
    if inner_type == "function_call":
        return (inner.get("name") or "",
                inner.get("arguments") or "{}",
                inner.get("call_id") or inner.get("id") or "")
    # flat form (in case codex ever writes without the envelope)
    if evt.get("type") == "function_call":
        return (evt.get("name") or "",
                evt.get("arguments") or "{}",
                evt.get("call_id") or evt.get("id") or "")
    return None


def _extract_function_call_output(evt: dict) -> Optional[Tuple[str, str]]:
    """Return (call_id, output_text) if this event is a tool-call result.

    Real codex v0.146 output shape::

        {"payload":{"type":"custom_tool_call_output","call_id":"...",
                    "output":[{"type":"input_text","text":"..."}, ...]}}

    We concatenate the pieces so downstream regex scanning sees one blob.
    """
    inner = _unwrap_event(evt)
    inner_type = inner.get("type")
    if inner_type in ("custom_tool_call_output", "function_call_output"):
        out = inner.get("output")
        return (inner.get("call_id") or "", _flatten_output(out))
    # Flat form
    if evt.get("type") in ("custom_tool_call_output", "function_call_output"):
        return (evt.get("call_id") or "", _flatten_output(evt.get("output")))
    return None


def _flatten_output(raw) -> str:
    """Codex tool output can be a plain string OR a list of ``{type,text}``.

    Turn any of those into a single string for path scanning.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                # {type: input_text/output_text, text: "..."}
                s = item.get("text") or item.get("content") or item.get("output") or ""
                if isinstance(s, str):
                    parts.append(s)
        return "\n".join(parts)
    return str(raw)


def _extract_turn_cwd(evt: dict) -> Optional[str]:
    """If *evt* is a turn_context or session_meta with a cwd, return it.

    codex v0.146 puts the real agent cwd here — no need to scan /proc.
    """
    if evt.get("type") in ("turn_context", "session_meta"):
        inner = _unwrap_event(evt)
        cwd = inner.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            return cwd.strip()
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
    # The rollout may declare its own cwd (turn_context.cwd / session_meta.cwd);
    # use the most recent one as the default base when resolving relatives.
    rollout_cwd: Optional[str] = None

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

            # Track the agent's declared cwd (turn_context / session_meta) so
            # later function_call_output rows resolve their relative paths
            # against the right base — codex records this, we shouldn't guess.
            declared_cwd = _extract_turn_cwd(evt)
            if declared_cwd:
                rollout_cwd = declared_cwd
                continue

            fc = _extract_function_call(evt)
            if fc is not None:
                name, args_raw, call_id = fc
                paths, base = _paths_from_function_call(name, args_raw)
                _remember(paths)
                # Prefer the tool's own cwd arg; fall back to rollout cwd.
                effective_base = base or rollout_cwd
                if call_id and effective_base:
                    call_bases[call_id] = effective_base
                continue

            fco = _extract_function_call_output(evt)
            if fco is not None:
                call_id, out = fco
                base = call_bases.get(call_id) or rollout_cwd
                _remember(_paths_from_result_output(out, base))
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
                    effective_base = base or rollout_cwd
                    if call_id and effective_base:
                        call_bases[call_id] = effective_base

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
