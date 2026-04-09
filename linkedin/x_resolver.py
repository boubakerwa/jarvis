"""
X (Twitter) post resolver — ported from x-ticker-investment/src/xPostResolver.js.

Fetches tweet content from public Twitter/X APIs (no auth required):
  1. Syndication API  — cdn.syndication.twimg.com  (primary, rich data)
  2. oEmbed API       — publish.twitter.com/oembed  (fallback, limited data)
"""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_X_STATUS_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:x\.com|twitter\.com)/[^/\s]+/status/(\d+)(?:\?[^\s]*)?",
    re.IGNORECASE,
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_PARAGRAPH_RE = re.compile(r"<p[^>]*>([\s\S]*?)</p>", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def _normalize_whitespace(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _strip_html(value: str) -> str:
    decoded = html.unescape(str(value or ""))
    return _normalize_whitespace(_HTML_TAG_RE.sub(" ", decoded))


def _normalize_url_token(value: str) -> str:
    return str(value or "").strip().rstrip("),.!?")


def _extract_text_from_oembed_html(html_str: str) -> str:
    match = _PARAGRAPH_RE.search(str(html_str or ""))
    return _normalize_whitespace(_strip_html(match.group(1) if match else ""))


def _replace_entity_urls(text: str, entities: dict) -> str:
    result = str(text or "")
    for entity in entities.get("urls") or []:
        token = str(entity.get("url") or "").strip()
        expanded = str(entity.get("expanded_url") or entity.get("url") or "").strip()
        if token:
            result = result.replace(token, expanded)
    for entity in entities.get("media") or []:
        token = str(entity.get("url") or "").strip()
        if token:
            result = result.replace(token, "")
    return _normalize_whitespace(result)


def _normalize_links(entities: dict) -> list[dict]:
    out = []
    for entity in entities.get("urls") or []:
        expanded = str(entity.get("expanded_url") or entity.get("url") or "").strip()
        if expanded:
            out.append({
                "displayUrl": str(entity.get("display_url") or "").strip(),
                "expandedUrl": expanded,
            })
    return out


def _normalize_media(payload: dict) -> list[dict]:
    media_details = payload.get("mediaDetails") or []
    photos = payload.get("photos") or []
    items = media_details if media_details else photos
    result = []
    for item in items:
        video_variants = (item.get("video_info") or {}).get("variants") or []
        mp4 = next(
            (v for v in video_variants if str(v.get("content_type") or "") == "video/mp4"),
            None,
        )
        raw_type = str(item.get("type") or ("video" if item.get("video_info") else ("photo" if item.get("media_url_https") else ""))).strip()
        sizes = (item.get("sizes") or {}).get("large") or {}
        original_info = item.get("original_info") or {}
        entry = {
            "type": raw_type or "media",
            "expandedUrl": str(item.get("expanded_url") or "").strip(),
            "previewUrl": str(item.get("media_url_https") or item.get("poster") or "").strip(),
            "assetUrl": str((mp4 or {}).get("url") or item.get("media_url_https") or "").strip(),
            "width": int(original_info.get("width") or sizes.get("w") or 0),
            "height": int(original_info.get("height") or sizes.get("h") or 0),
        }
        if entry["previewUrl"] or entry["assetUrl"] or entry["expandedUrl"]:
            result.append(entry)
    return result


def _build_media_summary(media: list[dict]) -> str:
    if not media:
        return "No media attached"
    counts: dict[str, int] = {}
    for item in media:
        t = str(item.get("type") or "media").lower().replace("_", " ")
        counts[t] = counts.get(t, 0) + 1
    parts = [f"{n} {t}{'s' if n != 1 else ''}" for t, n in counts.items()]
    return ", ".join(parts)


def _build_author_handle(url: str) -> str:
    match = re.match(
        r"^https?://(?:www\.)?(?:x\.com|twitter\.com)/([^/]+)/status/",
        str(url or ""),
        re.IGNORECASE,
    )
    return f"@{match.group(1)}" if match else ""


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _request_json(url: str, error_message: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            payload = json.loads(raw)
            return payload
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(error_message) from exc


# ---------------------------------------------------------------------------
# Source builders
# ---------------------------------------------------------------------------

def _build_syndication_source(payload: dict, x_url: str) -> dict:
    user = payload.get("user") or {}
    screen_name = str(user.get("screen_name") or "").strip()
    handle = f"@{screen_name}" if screen_name else _build_author_handle(x_url)
    canonical = f"https://x.com/{screen_name or _build_author_handle(x_url).lstrip('@')}/status/{payload.get('id_str', '')}"
    entities = payload.get("entities") or {}
    media = _normalize_media(payload)
    return {
        "type": "x-post",
        "xUrl": _normalize_url_token(x_url),
        "canonicalUrl": canonical,
        "postId": str(payload.get("id_str") or "").strip(),
        "extractionMethod": "syndication",
        "authorName": str(user.get("name") or "").strip(),
        "authorHandle": handle,
        "createdAt": str(payload.get("created_at") or "").strip(),
        "text": _replace_entity_urls(payload.get("text") or "", entities),
        "links": _normalize_links(entities),
        "media": media,
        "mediaSummary": _build_media_summary(media),
    }


def _build_oembed_source(payload: dict, x_url: str) -> dict:
    return {
        "type": "x-post",
        "xUrl": _normalize_url_token(x_url),
        "canonicalUrl": str(payload.get("url") or _normalize_url_token(x_url)).strip(),
        "postId": extract_x_post_id(x_url),
        "extractionMethod": "oembed",
        "authorName": str(payload.get("author_name") or "").strip(),
        "authorHandle": _build_author_handle(payload.get("url") or x_url),
        "createdAt": "",
        "text": _extract_text_from_oembed_html(payload.get("html") or ""),
        "links": [],
        "media": [],
        "mediaSummary": "Media preview unavailable",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_first_x_post_url(value: str) -> str:
    match = _X_STATUS_PATTERN.search(str(value or ""))
    return _normalize_url_token(match.group(0)) if match else ""


def extract_x_post_id(value: str) -> str:
    match = _X_STATUS_PATTERN.search(str(value or ""))
    return str(match.group(1)).strip() if match else ""


def is_x_post_url(value: str) -> bool:
    return bool(_X_STATUS_PATTERN.search(str(value or "")))


def resolve_x_post(x_url: str) -> dict:
    """
    Fetch tweet content from public X/Twitter APIs.

    Returns a source dict with: type, xUrl, canonicalUrl, postId,
    extractionMethod, authorName, authorHandle, createdAt, text,
    links, media, mediaSummary.

    Raises RuntimeError if neither API returns usable content.
    """
    normalized = _normalize_url_token(x_url)
    post_id = extract_x_post_id(normalized)

    if not post_id:
        raise RuntimeError("Paste a valid X post URL with /status/{id}.")

    # Primary: syndication API
    try:
        payload = _request_json(
            f"https://cdn.syndication.twimg.com/tweet-result?id={urllib.parse.quote(post_id)}&token=x",
            "Public X parsing failed for this post.",
        )
        return _build_syndication_source(payload, normalized)
    except RuntimeError:
        pass

    # Fallback: oEmbed API
    oembed_payload = _request_json(
        f"https://publish.twitter.com/oembed?omit_script=1&url={urllib.parse.quote(normalized)}",
        "Public X parsing failed for this post.",
    )
    source = _build_oembed_source(oembed_payload, normalized)

    if not source["text"]:
        raise RuntimeError(
            "This X post could not be parsed automatically. Paste the post text manually to continue."
        )

    return source
