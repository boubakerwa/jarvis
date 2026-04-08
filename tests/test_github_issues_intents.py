import unittest

from github_issues.intents import parse_issue_command


class GitHubIssueIntentTests(unittest.TestCase):
    def test_parse_create_with_labels(self):
        command = parse_issue_command("/gh create Inbox sync fails | Repro steps | labels=bug,ops")
        self.assertEqual(command.intent, "create")
        self.assertEqual(command.title, "Inbox sync fails")
        self.assertEqual(command.body, "Repro steps")
        self.assertEqual(command.labels, ("bug", "ops"))

    def test_parse_update_with_fields(self):
        command = parse_issue_command("/gh update 42 | state=closed | labels=done | title=Shipped")
        self.assertEqual(command.intent, "update")
        self.assertEqual(command.number, 42)
        self.assertEqual(command.state, "closed")
        self.assertEqual(command.labels, ("done",))
        self.assertEqual(command.title, "Shipped")

    def test_parse_list_defaults(self):
        command = parse_issue_command("/gh list")
        self.assertEqual(command.intent, "list")
        self.assertEqual(command.state, "open")
        self.assertEqual(command.limit, 5)

    def test_unknown_when_no_prefix(self):
        command = parse_issue_command("please create an issue")
        self.assertEqual(command.intent, "unknown")


if __name__ == "__main__":
    unittest.main()
