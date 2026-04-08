from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from github_issues.models import IssueSummary

RequestJson = Callable[[str, str, dict[str, str], Optional[dict[str, Any]]], Any]


class GitHubConfigError(RuntimeError):
    pass


class GitHubTokenMissingError(RuntimeError):
    pass


class GitHubAPIError(RuntimeError):
    def __init__(self, *, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class GitHubClientConfig:
    repository: str
    api_base: str = "https://api.github.com"
    token: str | None = None


def load_github_client_config(env: Mapping[str, str] | None = None) -> GitHubClientConfig:
    source = env if env is not None else os.environ
    repository = source.get("JARVIS_GITHUB_REPOSITORY", "").strip()
    if not repository:
        raise GitHubConfigError("Missing JARVIS_GITHUB_REPOSITORY (expected: owner/repo).")
    if "/" not in repository or repository.startswith("/") or repository.endswith("/"):
        raise GitHubConfigError("Invalid JARVIS_GITHUB_REPOSITORY format. Expected owner/repo.")

    api_base = source.get("JARVIS_GITHUB_API_BASE", "https://api.github.com").strip() or "https://api.github.com"
    token = source.get("JARVIS_GITHUB_TOKEN", "").strip() or source.get("GITHUB_TOKEN", "").strip() or None
    return GitHubClientConfig(repository=repository, api_base=api_base.rstrip("/"), token=token)


class GitHubIssuesClient:
    def __init__(self, config: GitHubClientConfig, request_json: RequestJson | None = None):
        self._config = config
        self._request_json = request_json or _default_request_json

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        request_json: RequestJson | None = None,
    ) -> "GitHubIssuesClient":
        return cls(load_github_client_config(env), request_json=request_json)

    def list_issues(self, *, state: str = "open", limit: int = 5) -> list[IssueSummary]:
        payload = self._request(
            "GET",
            f"/repos/{self._config.repository}/issues",
            query={"state": state, "per_page": str(max(1, min(limit, 50)))},
            require_auth=False,
        )
        if not isinstance(payload, list):
            raise GitHubAPIError(status=0, message="Unexpected GitHub response format for issue list.")
        items = [item for item in payload if isinstance(item, dict) and "pull_request" not in item]
        return [self._to_issue_summary(item) for item in items]

    def get_issue(self, number: int) -> IssueSummary:
        payload = self._request(
            "GET",
            f"/repos/{self._config.repository}/issues/{number}",
            require_auth=False,
        )
        if not isinstance(payload, dict):
            raise GitHubAPIError(status=0, message="Unexpected GitHub response format for issue detail.")
        return self._to_issue_summary(payload)

    def create_issue(
        self,
        *,
        title: str,
        body: str | None = None,
        labels: tuple[str, ...] | None = None,
    ) -> IssueSummary:
        request_body: dict[str, Any] = {"title": title}
        if body is not None:
            request_body["body"] = body
        if labels is not None:
            request_body["labels"] = list(labels)

        payload = self._request(
            "POST",
            f"/repos/{self._config.repository}/issues",
            payload=request_body,
            require_auth=True,
        )
        if not isinstance(payload, dict):
            raise GitHubAPIError(status=0, message="Unexpected GitHub response format for issue creation.")
        return self._to_issue_summary(payload)

    def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        labels: tuple[str, ...] | None = None,
        state: str | None = None,
    ) -> IssueSummary:
        request_body: dict[str, Any] = {}
        if title is not None:
            request_body["title"] = title
        if body is not None:
            request_body["body"] = body
        if labels is not None:
            request_body["labels"] = list(labels)
        if state is not None:
            request_body["state"] = state

        payload = self._request(
            "PATCH",
            f"/repos/{self._config.repository}/issues/{number}",
            payload=request_body,
            require_auth=True,
        )
        if not isinstance(payload, dict):
            raise GitHubAPIError(status=0, message="Unexpected GitHub response format for issue update.")
        return self._to_issue_summary(payload)

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        require_auth: bool,
    ) -> Any:
        if require_auth and not self._config.token:
            raise GitHubTokenMissingError(
                "GitHub token is required for this action. Set JARVIS_GITHUB_TOKEN or GITHUB_TOKEN."
            )

        url = f"{self._config.api_base}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "jarvis-github-issues-agent",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._config.token:
            headers["Authorization"] = f"Bearer {self._config.token}"

        return self._request_json(method, url, headers, payload)

    def _to_issue_summary(self, payload: dict[str, Any]) -> IssueSummary:
        labels_raw = payload.get("labels") if isinstance(payload.get("labels"), list) else []
        labels: list[str] = []
        for label in labels_raw:
            if isinstance(label, dict):
                name = str(label.get("name", "")).strip()
                if name:
                    labels.append(name)
            elif isinstance(label, str):
                cleaned = label.strip()
                if cleaned:
                    labels.append(cleaned)

        assignees_raw = payload.get("assignees") if isinstance(payload.get("assignees"), list) else []
        assignees: list[str] = []
        for assignee in assignees_raw:
            if isinstance(assignee, dict):
                login = str(assignee.get("login", "")).strip()
                if login:
                    assignees.append(login)

        return IssueSummary(
            number=int(payload.get("number", 0)),
            title=str(payload.get("title", "")).strip(),
            state=str(payload.get("state", "")).strip(),
            url=str(payload.get("html_url", "")).strip(),
            labels=tuple(labels),
            assignees=tuple(assignees),
            updated_at=str(payload.get("updated_at", "")).strip() or None,
        )


def _default_request_json(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
) -> Any:
    request_headers = dict(headers)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = Request(url=url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
    except HTTPError as exc:
        raw_error = exc.read().decode("utf-8", errors="replace")
        raise GitHubAPIError(status=exc.code, message=raw_error or str(exc)) from exc
    except URLError as exc:
        raise GitHubAPIError(status=0, message=str(exc.reason)) from exc
