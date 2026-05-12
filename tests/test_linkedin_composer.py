import unittest

from linkedin.composer import format_ready_for_telegram


class LinkedInComposerTelegramTests(unittest.TestCase):
    def test_ready_notification_does_not_include_generated_post_body(self):
        row = {
            "id": "draft-12345678",
            "voice": "operator",
            "pillar_label": "AI Efficiency",
            "source_author": "@source",
            "obsidian_filename": "ai-efficiency_draft-1234",
            "obsidian_path": "LinkedIn/2026-05/ai-efficiency_draft-1234.md",
        }
        draft = {
            "headline": "Latency is now strategy",
            "fullPost": "This is the generated LinkedIn post body that should stay out of Telegram.",
            "generation": {"mode": "draft"},
        }

        message = format_ready_for_telegram(row, draft)

        self.assertIn("LinkedIn draft ready", message)
        self.assertIn("Latency is now strategy", message)
        self.assertIn("ai-efficiency_draft-1234", message)
        self.assertNotIn("This is the generated LinkedIn post body", message)


if __name__ == "__main__":
    unittest.main()
