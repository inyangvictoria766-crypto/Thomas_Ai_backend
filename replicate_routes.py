

import os
import requests

from fastapi import HTTPException

from thomas_ai import (
    app,
    cloudinary,
    reserve_credits,
    refund_credits,
    record_generation
)

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")

REPLICATE_API_URL = "https://api.replicate.com/v1"

def replicate_headers():
    if not REPLICATE_API_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Replicate provider is not configured."
        )

    return {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json"
    }


@app.get("/replicate/status")
async def replicate_status():
    if not REPLICATE_API_TOKEN:
        return {"configured": False}

    return {"configured": True}
