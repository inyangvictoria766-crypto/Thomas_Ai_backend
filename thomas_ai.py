
import os
import requests
import psycopg2

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field


app = FastAPI(title="Thomas AI Backend")


# IMAGE REQUEST MODEL

class ImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)


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

    if not prompt:
        raise HTTPException(
            status_code=400,
            detail="Prompt cannot be empty."
        )

    api_key = os.getenv("STUDIOLM_API_KEY")
    print("API KEY LOADED:", bool(api_key), flush=True)
    print("API KEY LENGTH:", len(api_key) if api_key else 0, flush=True)

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="Image generation API key is missing."
        )

    try:
        response = requests.post(
            "https://api.studiolm.dev/v1/images/generations",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "prompt": prompt
            },
            timeout=120
        )

        response.raise_for_status()

        return response.json()

    except requests.RequestException as error:

        print(
            "STUDIOLM ERROR:",
            repr(error),
            flush=True
        )

        if error.response is not None:
            print(
                "STUDIOLM RESPONSE:",
                error.response.text,
                flush=True
            )

        raise HTTPException(
            status_code=502,
            detail="Image generation service failed. Check Render logs."
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
# IMAGE UPLOAD

@app.post("/upload-image")
async def upload_image(
    prompt: str = Form(...),
    image: UploadFile = File(...)
):

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

    return {
        "message": "Image received successfully!",
        "filename": image.filename,
        "image_size": len(image_data),
        "prompt": prompt,
        "status": "success"
    }


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
            "message": "Thomas AI is connected to PostgreSQL!",
            "database": result[0]
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
        
