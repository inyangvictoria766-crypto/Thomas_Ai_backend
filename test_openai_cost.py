
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import thomas_ai


class TestOpenAIImageCost(unittest.TestCase):

    def setUp(self):
        self.connection = MagicMock()
        cursor = self.connection.cursor.return_value.__enter__.return_value

        cursor.fetchall.return_value = [
            ("text_input", Decimal("5")),
            ("image_input", Decimal("8")),
            ("image_output", Decimal("30"))
        ]

        self.db_patch = patch(
            "thomas_ai.psycopg2.connect",
            return_value=self.connection
        )
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()

    def test_calculates_image_cost(self):
        usage = SimpleNamespace(
            input_tokens_details=SimpleNamespace(
                text_tokens=1000,
                image_tokens=2000
            ),
            output_tokens=3000
        )

        cost = thomas_ai.get_openai_image_cost(
            usage,
            "gpt-image-2.5-flare"
        )

        self.assertEqual(cost, Decimal("0.111"))

    def test_rejects_missing_usage(self):
        with self.assertRaises(ValueError):
            thomas_ai.get_openai_image_cost(
                None,
                "gpt-image-2.5-flare"
            )

    def test_rejects_negative_tokens(self):
        usage = SimpleNamespace(
            input_tokens_details=SimpleNamespace(
                text_tokens=-100,
                image_tokens=2000
            ),
            output_tokens=3000
        )

        with self.assertRaises(ValueError):
            thomas_ai.get_openai_image_cost(
                usage,
                "gpt-image-2.5-flare"
            )


if __name__ == "__main__":
    unittest.main()
      
