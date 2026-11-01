from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Thomas AI Backend")


class ImageRequest(BaseModel):
    prompt: str


@app.get("/")
def home():
    return {
        "message": "Thomas AI backend is running!",
        "status": "success"
    }


@app.post("/generate")
def generate_image(request: ImageRequest):
    return {
        "message": "Prompt received successfully!",
        "prompt": request.prompt,
        "status": "success"
    }
