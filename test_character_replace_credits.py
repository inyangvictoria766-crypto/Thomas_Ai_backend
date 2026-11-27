import unittest
import thomas_ai


class CharacterReplaceCreditTests(unittest.TestCase):

    def setUp(self):
        self.original_rate = (
            thomas_ai.PROVIDER_FUNDING_CONFIG["usd_to_ngn_rate"]
        )
        thomas_ai.PROVIDER_FUNDING_CONFIG["usd_to_ngn_rate"] = 1500

    def tearDown(self):
        thomas_ai.PROVIDER_FUNDING_CONFIG["usd_to_ngn_rate"] = (
            self.original_rate
        )

    def test_duration_credit_calculation(self):
    expected = {
        1: 3,
        5: 15,
        10: 30,
        17: 51,
        25: 75,
        30: 90
    }
        for duration, credits in expected.items():
            with self.subTest(duration=duration):
                self.assertEqual(
                    thomas_ai.calculate_character_replace_credits(duration),
                    credits
                )

    def test_duration_limits(self):
        self.assertEqual(
            thomas_ai.validate_character_video_duration(1), 1
        )
        self.assertEqual(
            thomas_ai.validate_character_video_duration(30), 30
        )

        for duration in [0, 0.99, 30.01, 31]:
            with self.subTest(duration=duration):
                with self.assertRaises(Exception):
                    thomas_ai.validate_character_video_duration(duration)

    def test_invalid_credit_durations(self):
        for duration in [0, 0.99, 30.01, 31]:
            with self.subTest(duration=duration):
                with self.assertRaises(ValueError):
                    thomas_ai.calculate_character_replace_credits(duration)


if __name__ == "__main__":
    unittest.main()
