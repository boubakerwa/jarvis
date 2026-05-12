from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_SOURCE_FILE_CHARS = 4000
DEFAULT_SOURCE_LINE_WINDOW = 160


def _project_root(root: Path | None = None) -> Path:
    return (root or ROOT).resolve()


def resolve_project_path(path: str, *, root: Path | None = None) -> Path:
    candidate_text = str(path or "").strip()
    if not candidate_text:
        raise ValueError("Path is required")

    project_root = _project_root(root)
    candidate = Path(candidate_text)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (project_root / candidate).resolve()

    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("Path must stay within the Marvis project root") from exc

    return resolved


def read_source_file(
    path: str,
    *,
    root: Path | None = None,
    max_chars: int = MAX_SOURCE_FILE_CHARS,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, str | bool | int | None]:
    resolved = resolve_project_path(path, root=root)
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not resolved.is_file():
        raise ValueError(f"Path is not a file: {path}")

    raw = resolved.read_bytes()
    if b"\x00" in raw[:1024]:
        raise ValueError(f"Binary files are not supported: {path}")

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total_lines = len(lines)

    actual_start_line: int | None = None
    actual_end_line: int | None = None
    if start_line is not None or end_line is not None:
        requested_start = max(1, int(start_line or 1))
        requested_end = int(end_line or min(requested_start + DEFAULT_SOURCE_LINE_WINDOW - 1, total_lines or requested_start))
        if requested_end < requested_start:
            raise ValueError("end_line must be greater than or equal to start_line")
        if total_lines > 0:
            actual_start_line = min(requested_start, total_lines)
            actual_end_line = min(requested_end, total_lines)
            text = "\n".join(lines[actual_start_line - 1 : actual_end_line])
        else:
            actual_start_line = requested_start
            actual_end_line = requested_start
            text = ""

    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    project_root = _project_root(root)
    relative_path = resolved.relative_to(project_root).as_posix()
    return {
        "path": relative_path,
        "content": text,
        "truncated": truncated,
        "start_line": actual_start_line,
        "end_line": actual_end_line,
        "total_lines": total_lines,
    }
