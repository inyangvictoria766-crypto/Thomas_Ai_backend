from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Thomas AI Backend")


class ImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)


@app.get("/")
def home():
    return {
        "message": "Thomas AI backend is running!",
        "status": "success"
    }


@app.post("/generate")
def generate_image(request: ImageRequest):
    prompt = request.prompt.strip()

    if not prompt:
        raise HTTPException(
            status_code=400,
            detail="Prompt cannot be empty."
        )

    return {
        "message": "Prompt received successfully!",
        "prompt": prompt,
        "status": "success"
    }


from fastapi import UploadFile, File, Form

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
import os
import psycopg2


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

    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Database connection failed."
        )
