from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_SOURCE_FILE_CHARS = 12000


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


def read_source_file(path: str, *, root: Path | None = None, max_chars: int = MAX_SOURCE_FILE_CHARS) -> dict[str, str | bool]:
    resolved = resolve_project_path(path, root=root)
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not resolved.is_file():
        raise ValueError(f"Path is not a file: {path}")

    raw = resolved.read_bytes()
    if b"\x00" in raw[:1024]:
        raise ValueError(f"Binary files are not supported: {path}")

    text = raw.decode("utf-8", errors="replace")
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
    }
