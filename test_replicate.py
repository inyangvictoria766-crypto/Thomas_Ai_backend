
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import thomas_ai
import replicate_routes


class FakeResponse:
    def __init__(self, data=None, content=b"fake-image-data"):
        self.data = data or {}
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class ReplicateTests(unittest.TestCase):

    def test_quality_levels(self):
        test_cases = [
            ("low", 1),
            ("medium", 4),
            ("high", 10),
        ]

        for quality, credits in test_cases:
            with self.subTest(quality=quality):

                request = thomas_ai.ImageRequest(
                    prompt="A beautiful mountain landscape",
                    quality=quality,
                    confirmed_credits=credits
                )

                current_user = {
                    "user_id": "test-user"
                }

                prediction_response = FakeResponse({
                    "id": "test-prediction"
                })

                completed_response = FakeResponse({
                    "id": "test-prediction",
                    "status": "succeeded",
                    "output": ["https://example.com/test.png"],
                    "metrics": {}
                })

                image_response = FakeResponse(
                    content=b"fake-image-data"
                )

                with patch.object(
                    thomas_ai,
                    "reserve_credits",
                    return_value=(99, "test-reservation")
                ) as reserve_credits, patch.object(
                    thomas_ai,
                    "reserve_provider_budget",
                    return_value=True
                ) as reserve_budget, patch.object(
                    thomas_ai,
                    "record_generation",
                    return_value=True
                ) as record_generation, patch.object(
                    replicate_routes.requests,
                    "post",
                    return_value=prediction_response
                ), patch.object(
                    replicate_routes.requests,
                    "get",
                    side_effect=[
                        completed_response,
                        image_response
                    ]
                ), patch.object(
                    replicate_routes,
                    "replicate_headers",
                    return_value={"Authorization": "Bearer test"}
                ), patch.object(
                    thomas_ai.cloudinary.uploader,
                    "upload",
                    return_value={
                        "secure_url": "https://cloudinary.com/test.png"
                    }
                ), patch.object(
                    replicate_routes.time,
                    "sleep"
                ):

                    result = replicate_routes.replicate_generate(
                        request,
                        current_user
                    )

                self.assertEqual(result["status"], "success")
                self.assertEqual(result["credits_used"], credits)
                self.assertEqual(result["quality"], quality)

                reserve_credits.assert_called_once()
                reserve_budget.assert_called_once()
                record_generation.assert_called_once()

    def test_budget_unavailable_refunds_customer(self):

        request = thomas_ai.ImageRequest(
            prompt="Test image",
            quality="low",
            confirmed_credits=1
        )

        current_user = {
            "user_id": "test-user"
        }

        with patch.object(
            thomas_ai,
            "reserve_credits",
            return_value=(99, "test-reservation")
        ), patch.object(
            thomas_ai,
            "reserve_provider_budget",
            return_value=False
        ), patch.object(
            thomas_ai,
            "refund_credits"
        ) as refund:

            with self.assertRaises(HTTPException) as error:
                replicate_routes.replicate_generate(
                    request,
                    current_user
                )

        self.assertEqual(error.exception.status_code, 503)
        refund.assert_called_once()

    def test_credit_confirmation_required(self):

        request = thomas_ai.ImageRequest(
            prompt="Test image",
            quality="medium",
            confirmed_credits=0
        )

        current_user = {
            "user_id": "test-user"
        }

        with patch.object(
            thomas_ai,
            "reserve_credits"
        ) as reserve_credits:

            with self.assertRaises(HTTPException) as error:
                replicate_routes.replicate_generate(
                    request,
                    current_user
                )

        self.assertEqual(error.exception.status_code, 409)
        reserve_credits.assert_not_called()


if __name__ == "__main__":
    unittest.main()
              
