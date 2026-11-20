
import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
from fastapi import HTTPException

import thomas_ai


class GenerateEndpointTests(unittest.TestCase):

    def setUp(self):
        self.user = {
            "user_id": "test-user",
            "credits": 100
        }

        self.request = thomas_ai.ImageRequest(
            prompt="A futuristic city",
            quality="high"
        )

        self.patches = [
            patch.object(
                thomas_ai, "reserve_credits",
                return_value=(97, "test-reservation")
            ),
            patch.object(
                thomas_ai, "reserve_provider_budget",
                return_value=True
            ),
            patch.object(
                thomas_ai, "record_generation",
                return_value=True
            ),
            patch.object(thomas_ai, "refund_credits"),
            patch.object(thomas_ai, "release_provider_budget"),
        ]

        self.mocks = [p.start() for p in self.patches]

        (
            self.reserve_credits,
            self.reserve_budget,
            self.record_generation,
            self.refund_credits,
            self.release_budget
        ) = self.mocks

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def make_openai(self, image="fake-image-data"):
        client = MagicMock()

        client.images.generate.return_value = SimpleNamespace(
            data=[
                SimpleNamespace(b64_json=image)
            ],
            usage={}
        )

        return client

    @patch.object(thomas_ai, "get_openai_image_cost")
    @patch.object(thomas_ai, "OpenAI")
    def test_openai_success(self, mock_openai, mock_cost):
        mock_openai.return_value = self.make_openai()
        mock_cost.return_value = 0.01

        result = thomas_ai.generate_image(
            self.request,
            current_user=self.user
        )

        self.assertIn("fake-image-data", str(result))
        self.refund_credits.assert_not_called()
        self.release_budget.assert_not_called()
        self.record_generation.assert_called_once()

    @patch.object(thomas_ai.requests, "post")
    @patch.object(thomas_ai, "get_cloudflare_generation_cost")
    def test_cloudflare_success(self, mock_cost, mock_post):
        self.request.quality = "low"

        response = MagicMock()
        response.json.return_value = {
            "success": True,
            "result": {
                "image": "fake-cloudflare-image"
            }
        }

        mock_post.return_value = response
        mock_cost.return_value = 0.001

        result = thomas_ai.generate_image(
            self.request,
            current_user=self.user
        )

        self.assertIn("fake-cloudflare-image", str(result))
        self.refund_credits.assert_not_called()
        self.release_budget.assert_not_called()

    @patch.object(thomas_ai, "OpenAI")
    def test_provider_failure_refunds_customer(self, mock_openai):
        client = MagicMock()
        client.images.generate.side_effect = RuntimeError(
            "Simulated provider failure"
        )
        mock_openai.return_value = client

        with self.assertRaises(HTTPException):
            thomas_ai.generate_image(
                self.request,
                current_user=self.user
            )

        self.refund_credits.assert_called_once()
        self.release_budget.assert_called_once()

    @patch.object(thomas_ai, "get_openai_image_cost")
    @patch.object(thomas_ai, "OpenAI")
    def test_accounting_failure_does_not_refund_successful_generation(
        self, mock_openai, mock_cost
    ):
        mock_openai.return_value = self.make_openai()
        mock_cost.return_value = 0.01

        self.record_generation.side_effect = RuntimeError(
            "Simulated accounting failure"
        )

        result = thomas_ai.generate_image(
            self.request,
            current_user=self.user
        )

        self.assertIn("fake-image-data", str(result))
        self.refund_credits.assert_not_called()
        self.release_budget.assert_not_called()

    def test_insufficient_customer_credits(self):
        self.reserve_credits.side_effect = HTTPException(
            status_code=402,
            detail="Insufficient credits"
        )

        with self.assertRaises(HTTPException) as error:
            thomas_ai.generate_image(
                self.request,
                current_user=self.user
            )

        self.assertEqual(error.exception.status_code, 402)
        self.reserve_budget.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
      
