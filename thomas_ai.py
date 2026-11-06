
import os
import io
import requests
import psycopg2
import base64

from openai import OpenAI
from PIL import Image
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field


app = FastAPI(title="Thomas AI Backend")


# IMAGE REQUEST MODEL

class ImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)
    quality: str = "low"

# HOME ROUTE

@app.get("/")
def home():
    return {
        "message": "Thomas AI backend is running!",
        "status": "success"
    }



# IMAGE GENERATION


@app.post("/generate")
def generate_image(request: ImageRequest):

    prompt = request.prompt.strip()
    quality = request.quality.lower().strip()

    if not prompt:
        raise HTTPException(
            status_code=400,
            detail="Prompt cannot be empty."
        )

    if quality not in ["low", "medium", "high"]:
        raise HTTPException(
            status_code=400,
            detail="Invalid image quality."
        )

    # HIGH QUALITY: OPENAI
    if quality == "high":

        api_key = os.getenv("OPENAI_API_KEY")

        if not api_key:
            raise HTTPException(
                status_code=500,
                detail="OpenAI API key is missing."
            )

        try:
            client = OpenAI(
                api_key=api_key,
                timeout=180.0,
                max_retries=1
            )

            response = client.images.generate(
                model="gpt-image-2.5-flare",
                prompt=prompt,
                quality="high",
                size="1024x1024",
                output_format="png",
                n=1
            )

            image_data = response.data[0].b64_json

            if not image_data:
                raise HTTPException(
                    status_code=502,
                    detail="OpenAI returned no image."
                )

            return {
                "image": image_data,
                "provider": "openai",
                "quality": "high",
                "status": "success"
            }

        except HTTPException:
            raise

        except Exception as error:

            print(
                "OPENAI IMAGE ERROR:",
                repr(error),
                flush=True
            )

            raise HTTPException(
                status_code=502,
                detail="OpenAI image generation failed."
            )

    # LOW AND MEDIUM: CLOUDFLARE

    api_token = os.getenv("CLOUDFLARE_API_TOKEN")
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")

    if not api_token or not account_id:
        raise HTTPException(
            status_code=500,
            detail="Cloudflare credentials are missing."
        )

    try:
        response = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/black-forest-labs/flux-1-schnell",
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json"
            },
            json={
                "prompt": prompt
            },
            timeout=150
        )

        response.raise_for_status()

        result = response.json()

        if not result.get("success"):
            raise HTTPException(
                status_code=502,
                detail="Cloudflare image generation failed."
            )

        image_data = result["result"]["image"]

        return {
            "image": image_data,
            "provider": "cloudflare",
            "quality": quality,
            "status": "success"
        }

    except HTTPException:
        raise

    except requests.RequestException as error:

        print(
            "CLOUDFLARE ERROR:",
            repr(error),
            flush=True
        )

        raise HTTPException(
            status_code=502,
            detail="Cloudflare image generation failed."
        )
            

@app.get("/studiolm-check")
def studiolm_check():
    api_key = os.getenv("STUDIOLM_API_KEY")

    if not api_key:
        return {"status": "error", "message": "API key missing"}

    try:
        response = requests.get(
            "https://api.studiolm.dev/v1/models",
            headers={
                "Authorization": f"Bearer {api_key}"
            },
            timeout=30
        )

        return {
            "http_status": response.status_code,
            "authenticated": response.status_code == 200,
            "response": response.json()
            if response.status_code == 200
            else "Authentication or API request failed"
        }

    except requests.RequestException as error:
        print("MODEL CHECK ERROR:", repr(error), flush=True)

        raise HTTPException(
            status_code=502,
            detail="Could not reach StudioLM."
            )


# IMAGE UPLOAD AND AI EDITING

@app.post("/upload-image")
async def upload_image(
    prompt: str = Form(...),
    quality: str = Form("low"),
    image: UploadFile = File(...)
):
    prompt = prompt.strip()
    quality = quality.lower().strip()

    if quality not in ["low", "medium", "high"]:
        raise HTTPException(
            status_code=400,
            detail="Quality must be low, medium, or high."
    )
    

    if not prompt:
        raise HTTPException(
            status_code=400,
            detail="Please enter an editing prompt."
        )

    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail="Please upload a valid image."
        )

    image_data = await image.read()

    if not image_data:
        raise HTTPException(
            status_code=400,
            detail="The uploaded image is empty."
        )

    if len(image_data) > 10 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail="Image must be 10 MB or smaller."
        )

    api_token = os.getenv("CLOUDFLARE_API_TOKEN")
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")

    if not api_token or not account_id:
        raise HTTPException(
            status_code=500,
            detail="Cloudflare credentials are missing."
        )

    try:
        # Resize uploaded image
        original = Image.open(io.BytesIO(image_data))
        original = original.convert("RGB")
        original.thumbnail((511, 511))

        width = max(256, (original.width // 8) * 8)
        height = max(256, (original.height // 8) * 8)

        original = original.resize((width, height))

        image_buffer = io.BytesIO()
        original.save(image_buffer, format="JPEG", quality=90)

        # LOW quality uses FLUX
        if quality != "low":
            raise HTTPException(
                status_code=501,
                detail="This quality option is not configured yet."
            )

        model = "@cf/black-forest-labs/flux-2-klein-4b"

        url = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{account_id}/ai/run/{model}"
        )

        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_token}"
            },
            data={
                "prompt": prompt,
                "width": width,
                "height": height
            },
            files={
                "input_image_0": (
                    "uploaded.jpg",
                    image_buffer.getvalue(),
                    "image/jpeg"
                )
            },
            timeout=180
        )

        response.raise_for_status()

    

         # Handle the returned image
        if response.headers.get("content-type", "").startswith("image/"):
            generated_image = base64.b64encode(
                response.content
            ).decode("utf-8")
        else:
            result = response.json()

            if not result.get("success"):
                raise HTTPException(
                    status_code=502,
                    detail=str(result.get("errors", "Image editing failed."))
                )

            output = result.get("result")

            if isinstance(output, dict):
                generated_image = output.get("image")
            elif isinstance(output, str):
                generated_image = output
            else:
                generated_image = None

        if not generated_image:
            raise HTTPException(
                status_code=502,
                detail="The model returned no image."
            )

        return {
            "image": generated_image,
            "status": "success",
            "message": "Image edited successfully!"
        }

        

    except HTTPException:
        raise

    except Exception as error:
        print(
            "IMAGE EDITING ERROR:",
            repr(error),
            flush=True
        )

        raise HTTPException(
            status_code=502,
            detail="Could not edit the uploaded image."
        )
        


# DATABASE CONNECTION CHECK

@app.get("/db-check")
def database_check():

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise HTTPException(
            status_code=500,
            detail="DATABASE_URL is missing."
        )

    try:
        connection = psycopg2.connect(database_url)

        cursor = connection.cursor()

        cursor.execute("SELECT version();")

        result = cursor.fetchone()

        cursor.close()
        connection.close()

        return {
            "status": "success",

        }

    except Exception as error:

        print(
            "DATABASE ERROR:",
            repr(error),
            flush=True
        )

        raise HTTPException(
            status_code=500,
            detail="Database connection failed."
    )
        
