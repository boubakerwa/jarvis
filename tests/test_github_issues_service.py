import unittest

from github_issues.client import GitHubTokenMissingError
from github_issues.models import IssueSummary
from github_issues.service import GitHubIssuesService


class _FakeClient:
    def __init__(self):
        self.created = []
        self.updated = []

    def create_issue(self, *, title, body=None, labels=None):
        self.created.append((title, body, labels))
        return IssueSummary(
            number=5,
            title=title,
            state="open",
            url="https://github.com/owner/repo/issues/5",
            labels=labels or (),
        )

    def get_issue(self, number):
        return IssueSummary(
            number=number,
            title="Tracked issue",
            state="open",
            url=f"https://github.com/owner/repo/issues/{number}",
            labels=("ops",),
            assignees=("wess",),
            updated_at="2026-04-06T12:00:00Z",
        )

    def list_issues(self, *, state, limit):
        return [
            IssueSummary(
                number=2,
                title="First",
                state=state,
                url="https://github.com/owner/repo/issues/2",
            ),
            IssueSummary(
                number=3,
                title="Second",
                state=state,
                url="https://github.com/owner/repo/issues/3",
            ),
        ][:limit]

    def update_issue(self, number, *, title=None, body=None, labels=None, state=None):
        self.updated.append((number, title, body, labels, state))
        return IssueSummary(
            number=number,
            title=title or "Updated issue",
            state=state or "open",
            url=f"https://github.com/owner/repo/issues/{number}",
            labels=labels or (),
        )


class _NoTokenClient(_FakeClient):
    def create_issue(self, *, title, body=None, labels=None):
        raise GitHubTokenMissingError("missing token")


class GitHubIssuesServiceTests(unittest.TestCase):
    def test_help_message(self):
        service = GitHubIssuesService(_FakeClient())
        response = service.handle_message("/gh help")
        self.assertEqual(response.intent, "help")
        self.assertIn("/gh create", response.text)

    def test_create_issue(self):
        client = _FakeClient()
        service = GitHubIssuesService(client)
        response = service.handle_message("/gh create Agent drift | add checks | labels=ops,bug")
        self.assertEqual(response.intent, "create")
        self.assertIsNotNone(response.issue)
        self.assertEqual(response.issue.number, 5)
        self.assertEqual(client.created[0][2], ("ops", "bug"))

    def test_status_issue(self):
        service = GitHubIssuesService(_FakeClient())
        response = service.handle_message("/gh status 42")
        self.assertEqual(response.intent, "status")
        self.assertIn("Issue #42", response.text)
        self.assertIn("Labels: ops", response.text)

    def test_list_issues(self):
        service = GitHubIssuesService(_FakeClient())
        response = service.handle_message("/gh list open 2")
        self.assertEqual(response.intent, "list")
        self.assertEqual(len(response.issues), 2)
        self.assertIn("Open issues (2)", response.text)

    def test_update_requires_fields(self):
        service = GitHubIssuesService(_FakeClient())
        response = service.handle_message("/gh update 9")
        self.assertEqual(response.intent, "update")
        self.assertIn("at least one update field", response.text)

    def test_missing_token_error_is_human_readable(self):
        service = GitHubIssuesService(_NoTokenClient())
        response = service.handle_message("/gh create Needs token")
        self.assertEqual(response.intent, "create")
        self.assertIn("missing token", response.text)


if __name__ == "__main__":
    unittest.main()
