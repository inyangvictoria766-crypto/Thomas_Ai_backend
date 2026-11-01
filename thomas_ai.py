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
