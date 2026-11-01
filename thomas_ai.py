from fastapi import FastAPI

app = FastAPI(title="Thomas AI Backend")

@app.get("/")
def home():
    return {
        "message": "Thomas AI backend is running!",
        "status": "success"
    }
