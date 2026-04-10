import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeGitHubClient:
    def create_issue(self, *, title, body=None, labels=None):
        return type(
            "Issue",
            (),
            {
                "number": 42,
                "title": title,
                "labels": labels or (),
                "url": "https://github.com/owner/repo/issues/42",
            },
        )()

    def list_pull_requests(self, *, state, limit):
        return [
            type(
                "PR",
                (),
                {
                    "number": 10,
                    "title": "Add proactive reminders",
                    "state": state,
                    "author": "wess",
                    "base_branch": "main",
                    "head_branch": "codex/reminders",
                    "updated_at": "2026-04-10T10:00:00Z",
                    "url": "https://github.com/owner/repo/pull/10",
                },
            )()
        ]

    def get_pull_request(self, number):
        return type(
            "PR",
            (),
            {
                "number": number,
                "title": "Add proactive reminders",
                "state": "open",
                "author": "wess",
                "base_branch": "main",
                "head_branch": "codex/reminders",
                "updated_at": "2026-04-10T10:00:00Z",
                "additions": 120,
                "deletions": 8,
                "changed_files": 6,
                "commit_count": 3,
                "body": "Implements proactive reminder delivery.",
                "url": "https://github.com/owner/repo/pull/10",
            },
        )()

    def list_commits(self, *, branch, limit):
        return [
            type(
                "Commit",
                (),
                {
                    "short_sha": "abc12345",
                    "message": "Add proactive reminders\n\nBody",
                    "author": "wess",
                    "committed_at": "2026-04-10T11:00:00Z",
                    "url": "https://github.com/owner/repo/commit/abc12345",
                },
            )()
        ]

    def get_commit(self, sha):
        return type(
            "Commit",
            (),
            {
                "short_sha": sha[:8],
                "message": "Add proactive reminders",
                "author": "wess",
                "committed_at": "2026-04-10T11:00:00Z",
                "additions": 20,
                "deletions": 4,
                "changed_files": 2,
                "files": ("core/agent.py", "reminders/service.py"),
                "url": "https://github.com/owner/repo/commit/abc12345",
            },
        )()


class AgentGitHubToolTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module("tested_agent_github_tools", "core/agent.py")

    def test_list_pull_requests_formats_response(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._github_client = lambda: FakeGitHubClient()

        response = agent._tool_list_pull_requests({"state": "open", "limit": 5})

        self.assertIn("PR #10", response)
        self.assertIn("codex/reminders -> main", response)

    def test_read_pull_request_formats_stats_and_body(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._github_client = lambda: FakeGitHubClient()

        response = agent._tool_read_pull_request({"number": 10})

        self.assertIn("Diff stats: 120 additions, 8 deletions, 6 files, 3 commits", response)
        self.assertIn("Implements proactive reminder delivery.", response)

    def test_list_commits_formats_headline(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._github_client = lambda: FakeGitHubClient()

        response = agent._tool_list_commits({"branch": "main", "limit": 5})

        self.assertIn("abc12345 Add proactive reminders", response)

    def test_read_commit_formats_files(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._github_client = lambda: FakeGitHubClient()

        response = agent._tool_read_commit({"sha": "abc12345"})

        self.assertIn("Diff stats: 20 additions, 4 deletions, 2 files", response)
        self.assertIn("core/agent.py", response)

    def test_create_github_issue_formats_response(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._github_client = lambda: FakeGitHubClient()

        response = agent._tool_create_github_issue(
            {
                "title": "FEATURE-006: Better GitHub triage",
                "body": "Add agent issue creation.",
                "labels": ["feature", "ops"],
            }
        )

        self.assertIn("Created GitHub issue #42", response)
        self.assertIn("[labels: feature, ops]", response)
        self.assertIn("https://github.com/owner/repo/issues/42", response)

    def test_create_github_issue_surfaces_config_errors(self):
        agent = self.module.JarvisAgent.__new__(self.module.JarvisAgent)
        agent._github_client = lambda: (_ for _ in ()).throw(self.module.GitHubConfigError("Missing repo config"))

        response = agent._tool_create_github_issue({"title": "Feature request"})

        self.assertIn("Could not create GitHub issue", response)


if __name__ == "__main__":
    unittest.main()
