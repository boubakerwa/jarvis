import unittest

from github_issues.client import (
    GitHubClientConfig,
    GitHubConfigError,
    GitHubIssuesClient,
    GitHubTokenMissingError,
    load_github_client_config,
)


class GitHubIssuesClientTests(unittest.TestCase):
    def test_load_config_requires_repository(self):
        with self.assertRaises(GitHubConfigError):
            load_github_client_config({})

    def test_load_config_supports_token_fallback(self):
        config = load_github_client_config(
            {
                "JARVIS_GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_TOKEN": "token-123",
            }
        )
        self.assertEqual(config.repository, "owner/repo")
        self.assertEqual(config.token, "token-123")

    def test_create_issue_requires_token(self):
        client = GitHubIssuesClient(
            GitHubClientConfig(repository="owner/repo", token=None),
            request_json=lambda *_args: {},
        )
        with self.assertRaises(GitHubTokenMissingError):
            client.create_issue(title="Missing auth")

    def test_list_issues_filters_pull_requests(self):
        captured = {}

        def fake_request(method, url, headers, payload):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = payload
            return [
                {
                    "number": 7,
                    "title": "Real issue",
                    "state": "open",
                    "html_url": "https://github.com/owner/repo/issues/7",
                    "labels": [{"name": "bug"}],
                },
                {
                    "number": 8,
                    "title": "PR masquerading in issues API",
                    "state": "open",
                    "html_url": "https://github.com/owner/repo/pull/8",
                    "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/8"},
                },
            ]

        client = GitHubIssuesClient(
            GitHubClientConfig(repository="owner/repo", token=None),
            request_json=fake_request,
        )
        issues = client.list_issues(state="open", limit=10)
        self.assertEqual(captured["method"], "GET")
        self.assertIn("/repos/owner/repo/issues", captured["url"])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].number, 7)
        self.assertEqual(issues[0].labels, ("bug",))

    def test_create_issue_payload_and_auth_header(self):
        captured = {}

        def fake_request(method, url, headers, payload):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = payload
            return {
                "number": 12,
                "title": "Automation drift",
                "state": "open",
                "html_url": "https://github.com/owner/repo/issues/12",
                "labels": [{"name": "ops"}],
            }

        client = GitHubIssuesClient(
            GitHubClientConfig(repository="owner/repo", token="secret-token"),
            request_json=fake_request,
        )
        issue = client.create_issue(title="Automation drift", body="Details", labels=("ops",))
        self.assertEqual(captured["method"], "POST")
        self.assertIn("/repos/owner/repo/issues", captured["url"])
        self.assertEqual(captured["payload"]["title"], "Automation drift")
        self.assertEqual(captured["payload"]["labels"], ["ops"])
        self.assertIn("Authorization", captured["headers"])
        self.assertEqual(issue.number, 12)


if __name__ == "__main__":
    unittest.main()
