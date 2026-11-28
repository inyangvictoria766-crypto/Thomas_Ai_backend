
import os
import time
import requests

from decimal import Decimal
from fastapi import HTTPException, Depends
from fastapi.security import HTTPAuthorizationCredentials

import thomas_ai

app = thomas_ai.app

REPLICATE_API_URL = "https://api.replicate.com/v1"
REPLICATE_MODEL = "openai/gpt-image-2"

REPLICATE_PRICES = {
    "low": Decimal("0.012"),
    "medium": Decimal("0.047"),
    "high": Decimal("0.128")
}

# Provider-cost reservation includes a small safety margin.
REPLICATE_RESERVATION_MULTIPLIER = Decimal("1.10")


def replicate_headers():
    token = os.getenv("REPLICATE_API_TOKEN")

    if not token:
        raise HTTPException(
            status_code=503,
            detail="Replicate provider is not configured."
        )

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }


@app.get("/replicate/status")
def replicate_status():
    return {
        "configured": bool(os.getenv("REPLICATE_API_TOKEN")),
        "provider": "replicate",
        "model": REPLICATE_MODEL
    }


@app.post("/replicate/generate")
def replicate_generate(
    request: thomas_ai.ImageRequest,
    current_user: dict = Depends(
        thomas_ai.get_authenticated_user
    )
):
    started = time.perf_counter()

    prompt = request.prompt.strip()
    quality = request.quality.lower().strip()

    if not prompt:
        raise HTTPException(
            status_code=400,
            detail="Prompt cannot be empty."
        )

    if quality not in REPLICATE_PRICES:
        raise HTTPException(
            status_code=400,
            detail="Invalid image quality."
        )

    # Customer credit confirmation.
    # Prices are based on the ₦20 provider allowance per credit.
    expected_credits = {
        "low": 1,
        "medium": 4,
        "high": 10
    }[quality]

    if request.confirmed_credits != expected_credits:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Credit confirmation required.",
                "required_credits": expected_credits
            }
        )

    credits_used = expected_credits

    new_balance, reservation_id = (
        thomas_ai.reserve_credits(
            current_user["user_id"],
            credits_used,
            quality
        )
    )

    provider_cost_estimate = (
        REPLICATE_PRICES[quality]
        * REPLICATE_RESERVATION_MULTIPLIER
    )

    try:
        budget_reserved = (
            thomas_ai.reserve_provider_budget(
                "replicate",
                reservation_id,
                provider_cost_estimate
            )
        )

    except Exception as error:
        print(
            "REPLICATE BUDGET ERROR:",
            repr(error),
            flush=True
        )

        thomas_ai.refund_credits(
            current_user["user_id"],
            reservation_id,
            quality
        )

        raise HTTPException(
            status_code=503,
            detail="Replicate budget reservation failed."
        )

    if not budget_reserved:
        thomas_ai.refund_credits(
            current_user["user_id"],
            reservation_id,
            quality
        )

        raise HTTPException(
            status_code=503,
            detail="Replicate generation budget unavailable."
        )

    generation_succeeded = False

    try:
    response = requests.post(
        f"{REPLICATE_API_URL}/models/{REPLICATE_MODEL}/predictions",
        headers=replicate_headers(),
        json={
            "input": {
                "prompt": prompt,
                "quality": quality,
                "aspect_ratio": "1:1",
                "output_format": "png",
                "number_of_images": 1,
                "moderation": "auto",
                "background": "opaque"
            }
        },
        timeout=60
    )

        response.raise_for_status()
        prediction = response.json()

        prediction_id = prediction.get("id")

        if not prediction_id:
            raise RuntimeError(
                "Replicate returned no prediction ID."
            )

        # Poll until the prediction finishes.
        deadline = time.time() + 240

        while time.time() < deadline:
            check = requests.get(
                f"{REPLICATE_API_URL}/predictions/{prediction_id}",
                headers=replicate_headers(),
                timeout=30
            )

            check.raise_for_status()
            prediction = check.json()

            status = prediction.get("status")

            if status == "succeeded":
                generation_succeeded = True
                break

            if status in ("failed", "canceled"):
                raise RuntimeError(
                    prediction.get("error")
                    or f"Replicate prediction {status}."
                )

            time.sleep(2)

        output = prediction.get("output")
        image_url = None

        if isinstance(output, list):
            if output:
                image_url = output[0]
            output_count = len(output)
        else:
            image_url = output
            output_count = 1 if output else 0

        if not image_url:
            raise RuntimeError(
                "Replicate returned no image."
            )

        

        # Download the temporary Replicate output.
        image_response = requests.get(
            image_url,
            timeout=90
        )

        image_response.raise_for_status()

        image_data = image_response.content

        # Upload to permanent Cloudinary storage.
        upload = thomas_ai.cloudinary.uploader.upload(
            image_data,
            folder="thomas_ai/generated",
            resource_type="image"
        )

        permanent_url = upload.get("secure_url")

        if not permanent_url:
            raise RuntimeError(
                "Cloudinary returned no image URL."
            )
    
        
        metrics = prediction.get("metrics") or {}

        model_variant = (
            metrics.get("model_variant") or quality
        )

        actual_output_count = (
            metrics.get("image_output_count") or output_count
        )

        accounting_recorded = False

        if model_variant in REPLICATE_PRICES:
            provider_cost = (
                REPLICATE_PRICES[model_variant]
                * Decimal(str(actual_output_count))
            )

            try:
                accounting_recorded = thomas_ai.record_generation(
                    current_user["user_id"],
                    quality,
                    "replicate",
                    reservation_id,
                    REPLICATE_MODEL,
                    provider_cost
                )
            except Exception as error:
                print(
                    "REPLICATE ACCOUNTING ERROR:",
                    repr(error),
                    flush=True
                )

            print(
                "REPLICATE COST:",
                str(provider_cost),
                "USD",
                flush=True
            )
        else:
            print(
                "UNKNOWN REPLICATE MODEL VARIANT:",
                model_variant,
                flush=True
            )
            


        print(
            
            "REPLICATE METRICS:",
            prediction.get("metrics"),
            flush=True
        )
        print(
            "REPLICATE PREDICTION:",
            prediction_id,
            flush=True
        )

        print(
             "REPLICATE OUTPUT STORED:",
            permanent_url,
           flush=True
        )

        total_seconds = time.perf_counter() - started

        return {
            "image": permanent_url,
            "provider": "replicate",
            "model": REPLICATE_MODEL,
            "quality": quality,
            "credits_used": credits_used,
            "remaining_credits": new_balance,
            "status": "success",
            "generation_seconds": round(total_seconds, 2),
            "accounting_status": "pending_cost_verification"
        }

    except Exception as error:
        print(
            "REPLICATE GENERATION ERROR:",
            repr(error),
            flush=True
        )

        if not generation_succeeded:
            try:
                thomas_ai.release_provider_budget(
                    reservation_id
                )
            except Exception as release_error:
                print(
                    "REPLICATE RELEASE ERROR:",
                    repr(release_error),
                    flush=True
                )

            thomas_ai.refund_credits(
                current_user["user_id"],
                reservation_id,
                quality
            )

        raise HTTPException(
            status_code=502,
            detail="Replicate image generation failed."
    )
    
