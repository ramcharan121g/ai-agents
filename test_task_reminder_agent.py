import unittest

from task_reminder_agent import build_reminder_lines, due_soon, task_hash


class TaskReminderAgentTests(unittest.TestCase):
    def test_due_soon_without_due_date(self) -> None:
        self.assertTrue(due_soon(None, 24))

    def test_task_hash_stable(self) -> None:
        first = task_hash({"title": "Submit report", "source": "gmail", "source_id": "abc"})
        second = task_hash({"title": " submit report ", "source": "GMAIL", "source_id": "ABC"})
        self.assertEqual(first, second)

    def test_build_reminder_lines_filters_future_tasks(self) -> None:
        tasks = [
            {
                "title": "Do now",
                "priority": "high",
                "source": "slack",
                "due_at": None,
            },
            {
                "title": "Do next week",
                "priority": "low",
                "source": "gmail",
                "due_at": "2100-01-01T00:00:00Z",
            },
        ]
        lines = build_reminder_lines(tasks, 24)
        self.assertEqual(len(lines), 1)
        self.assertIn("Do now", lines[0])


if __name__ == "__main__":
    unittest.main()
