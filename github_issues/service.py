from __future__ import annotations

from github_issues.client import GitHubAPIError, GitHubTokenMissingError, GitHubIssuesClient
from github_issues.intents import parse_issue_command
from github_issues.models import IssueAgentResponse, IssueSummary

HELP_TEXT = (
    "GitHub issue commands:\n"
    "- /gh create <title> | <body> | labels=bug,ops\n"
    "- /gh status <issue-number>\n"
    "- /gh list [open|closed|all] [limit]\n"
    "- /gh update <issue-number> | title=... | body=... | labels=... | state=open|closed"
)


class GitHubIssuesService:
    def __init__(self, client: GitHubIssuesClient):
        self._client = client

    def handle_message(self, text: str) -> IssueAgentResponse:
        command = parse_issue_command(text)

        if command.intent == "help":
            return IssueAgentResponse(text=HELP_TEXT, intent=command.intent)
        if command.intent == "unknown":
            return IssueAgentResponse(
                text="I could not parse that GitHub command.\n\n" + HELP_TEXT,
                intent=command.intent,
            )

        try:
            if command.intent == "create":
                return self._handle_create(command.title, command.body, command.labels)
            if command.intent == "status":
                return self._handle_status(command.number)
            if command.intent == "list":
                return self._handle_list(command.state or "open", command.limit or 5)
            if command.intent == "update":
                return self._handle_update(
                    number=command.number,
                    title=command.title,
                    body=command.body,
                    labels=command.labels,
                    state=command.state,
                )
        except GitHubTokenMissingError as exc:
            return IssueAgentResponse(
                text=(
                    f"{exc}\n"
                    "Once token + repo are configured, I can create/update issues directly from Telegram commands."
                ),
                intent=command.intent,
            )
        except GitHubAPIError as exc:
            return IssueAgentResponse(
                text=f"GitHub API error ({exc.status}): {exc.message}",
                intent=command.intent,
            )

        return IssueAgentResponse(text="Unsupported GitHub command.", intent=command.intent)

    def _handle_create(
        self,
        title: str | None,
        body: str | None,
        labels: tuple[str, ...] | None,
    ) -> IssueAgentResponse:
        cleaned_title = (title or "").strip()
        if not cleaned_title:
            return IssueAgentResponse(
                text="Please provide a title. Example: /gh create Billing bug | Steps... | labels=bug,finance",
                intent="create",
            )

        issue = self._client.create_issue(title=cleaned_title, body=body, labels=labels)
        return IssueAgentResponse(
            text=f"Created issue #{issue.number}: {issue.title}\n{issue.url}",
            intent="create",
            issue=issue,
        )

    def _handle_status(self, number: int | None) -> IssueAgentResponse:
        if number is None:
            return IssueAgentResponse(
                text="Please provide an issue number. Example: /gh status 42",
                intent="status",
            )
        issue = self._client.get_issue(number)
        return IssueAgentResponse(
            text=_format_issue(issue),
            intent="status",
            issue=issue,
        )

    def _handle_list(self, state: str, limit: int) -> IssueAgentResponse:
        issues = tuple(self._client.list_issues(state=state, limit=limit))
        if not issues:
            return IssueAgentResponse(
                text=f"No {state} issues found.",
                intent="list",
                issues=issues,
            )

        lines = [f"{state.title()} issues ({len(issues)}):"]
        for issue in issues:
            lines.append(f"- #{issue.number} [{issue.state}] {issue.title}")
        return IssueAgentResponse(
            text="\n".join(lines),
            intent="list",
            issues=issues,
        )

    def _handle_update(
        self,
        *,
        number: int | None,
        title: str | None,
        body: str | None,
        labels: tuple[str, ...] | None,
        state: str | None,
    ) -> IssueAgentResponse:
        if number is None:
            return IssueAgentResponse(
                text="Please provide an issue number. Example: /gh update 42 | state=closed",
                intent="update",
            )
        if title is None and body is None and labels is None and state is None:
            return IssueAgentResponse(
                text=(
                    "Please provide at least one update field. "
                    "Example: /gh update 42 | state=closed | labels=done"
                ),
                intent="update",
            )

        issue = self._client.update_issue(
            number,
            title=title,
            body=body,
            labels=labels,
            state=state,
        )
        return IssueAgentResponse(
            text=f"Updated issue #{issue.number}: {issue.title}\n{issue.url}",
            intent="update",
            issue=issue,
        )


def _format_issue(issue: IssueSummary) -> str:
    labels = ", ".join(issue.labels) if issue.labels else "none"
    assignees = ", ".join(issue.assignees) if issue.assignees else "none"
    lines = [
        f"Issue #{issue.number}: {issue.title}",
        f"State: {issue.state}",
        f"Labels: {labels}",
        f"Assignees: {assignees}",
        issue.url,
    ]
    if issue.updated_at:
        lines.insert(4, f"Updated: {issue.updated_at}")
    return "\n".join(lines)
