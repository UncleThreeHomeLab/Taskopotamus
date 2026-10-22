import unittest

from src.agent.compaction_policy import CompactionPolicy


class CompactionPolicyTests(unittest.TestCase):
    def test_shared_policy_uses_each_agents_window_independently(self):
        policy = CompactionPolicy(50)
        for window, expected in ((80000, 40000), (30000, 15000), (90001, 45000)):
            with self.subTest(window=window):
                self.assertEqual(policy.config(window), {
                    "model_auto_compact_token_limit": expected,
                    "model_auto_compact_token_limit_scope": "total",
                })

    def test_unknown_windows_use_runtime_defaults(self):
        for window in (None, 0, -1, True, "80000", 80000.5):
            with self.subTest(window=window):
                self.assertEqual(CompactionPolicy(50).config(window), {})

    def test_default_policy_leaves_threshold_to_runtime_even_with_known_window(self):
        self.assertEqual(CompactionPolicy().config(80000), {})

    def test_invalid_percentages_are_rejected(self):
        for percent in (0, -1, 101, True, 0.5, "50"):
            with self.subTest(percent=percent), self.assertRaises(ValueError):
                CompactionPolicy(percent)

    def test_custom_percentage_and_small_window_keep_positive_threshold(self):
        self.assertEqual(CompactionPolicy(25).config(90000)["model_auto_compact_token_limit"], 22500)
        self.assertEqual(CompactionPolicy(1).config(1)["model_auto_compact_token_limit"], 1)
        self.assertEqual(CompactionPolicy(100).config(90000)["model_auto_compact_token_limit"], 90000)


if __name__ == "__main__":
    unittest.main()
