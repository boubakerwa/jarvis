import unittest

from github_issues.client import (
    GitHubClientConfig,
    GitHubIssuesClient,
    GitHubTokenMissingError,
    load_github_client_config,
)
from github_issues.intents import parse_issue_command


class GitHubIssuesTests(unittest.TestCase):
    def test_load_github_client_config_reads_repository_and_token(self):
        config = load_github_client_config(
            {
                "JARVIS_GITHUB_REPOSITORY": "boubakerwa/jarvis",
                "JARVIS_GITHUB_TOKEN": "secret-token",
            }
        )

        self.assertEqual(config.repository, "boubakerwa/jarvis")
        self.assertEqual(config.api_base, "https://api.github.com")
        self.assertEqual(config.token, "secret-token")

    def test_parse_issue_command_handles_create_and_update(self):
        create = parse_issue_command("gh create Fix Gmail summaries | Add more context | labels=bug,ops")
        update = parse_issue_command("gh update 42 | state=closed | labels=done,ops | body=Shipped")

        self.assertEqual(create.intent, "create")
        self.assertEqual(create.title, "Fix Gmail summaries")
        self.assertEqual(create.labels, ("bug", "ops"))

        self.assertEqual(update.intent, "update")
        self.assertEqual(update.number, 42)
        self.assertEqual(update.state, "closed")
        self.assertEqual(update.labels, ("done", "ops"))
        self.assertEqual(update.body, "Shipped")

    def test_github_issues_client_builds_issue_list_and_create_requests(self):
        calls = []

        def fake_request(method, url, headers, payload):
            calls.append((method, url, headers, payload))
            if method == "GET":
                return [
                    {
                        "number": 7,
                        "title": "Inbox triage",
                        "state": "open",
                        "html_url": "https://github.com/boubakerwa/jarvis/issues/7",
                        "labels": [{"name": "ops"}],
                        "assignees": [{"login": "wess"}],
                        "updated_at": "2026-04-06T10:00:00Z",
                    },
                    {
                        "number": 8,
                        "title": "Ignore PRs",
                        "state": "open",
                        "html_url": "https://github.com/boubakerwa/jarvis/issues/8",
                        "pull_request": {},
                    },
                ]
            return {
                "number": 9,
                "title": payload["title"],
                "state": "open",
                "html_url": "https://github.com/boubakerwa/jarvis/issues/9",
                "labels": [{"name": "ops"}],
                "assignees": [],
                "updated_at": "2026-04-06T10:05:00Z",
            }

        client = GitHubIssuesClient(
            GitHubClientConfig(repository="boubakerwa/jarvis", token="secret-token"),
            request_json=fake_request,
        )

        issues = client.list_issues(limit=5)
        created = client.create_issue(title="Ship issue agent", body="Scaffold first", labels=("ops",))

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].number, 7)
        self.assertEqual(issues[0].labels, ("ops",))
        self.assertEqual(issues[0].assignees, ("wess",))
        self.assertEqual(created.number, 9)
        self.assertEqual(calls[0][0], "GET")
        self.assertIn("per_page=5", calls[0][1])
        self.assertEqual(calls[1][0], "POST")
        self.assertEqual(calls[1][3]["labels"], ["ops"])

    def test_create_issue_requires_token(self):
        client = GitHubIssuesClient(
            GitHubClientConfig(repository="boubakerwa/jarvis"),
            request_json=lambda method, url, headers, payload: {},
        )

        with self.assertRaises(GitHubTokenMissingError):
            client.create_issue(title="Missing token")


if __name__ == "__main__":
    unittest.main()
