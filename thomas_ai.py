import os
import io
import requests
import psycopg2
import base64
import time
import uuid
import secrets
import hashlib
from fastapi import Depends
from openai import OpenAI
from PIL import Image
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field


app = FastAPI(title="Thomas AI Backend")
CREDIT_COSTS = {
    "low": 1,
    "medium": 2,
    "high": 3
}
from fastapi import Header

def get_authenticated_user(
    authorization: str = Header(None)
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authentication required."
        )

    access_token = authorization.replace("Bearer ", "", 1).strip()
    token_hash = hashlib.sha256(
        access_token.encode()
    ).hexdigest()

    connection = None

    try:
        connection = psycopg2.connect(
            os.getenv("DATABASE_URL")
        )

        cursor = connection.cursor()

        cursor.execute("""
            SELECT user_id, credits
            FROM app_users
            WHERE token_hash = %s;
        """, (token_hash,))

        user = cursor.fetchone()

        cursor.close()

        if not user:
            raise HTTPException(
                status_code=401,
                detail="Invalid access token."
            )

        return {
            "user_id": str(user[0]),
            "credits": user[1]
        }

    finally:
        if connection:
            connection.close()

def reserve_credits(user_id, amount, quality):
    connection = None

    try:
        connection = psycopg2.connect(
            os.getenv("DATABASE_URL")
        )

        cursor = connection.cursor()

        # Deduct only if the balance is sufficient.
        cursor.execute("""
            UPDATE app_users
            SET credits = credits - %s
            WHERE user_id = %s
              AND credits >= %s
            RETURNING credits;
        """, (amount, user_id, amount))

        result = cursor.fetchone()

        if not result:
            cursor.execute("""
                SELECT credits
                FROM app_users
                WHERE user_id = %s;
            """, (user_id,))

            account = cursor.fetchone()

            if not account:
                raise HTTPException(
                    status_code=401,
                    detail="Account not found."
                )

            raise HTTPException(
                status_code=402,
                detail="Not enough credits."
            )

        new_balance = result[0]

        cursor.execute("""
            INSERT INTO credit_transactions
            (user_id, amount, transaction_type, description)
            VALUES (%s, %s, %s, %s);
        """, (
            user_id,
            -amount,
            "generation_reservation",
            f"{quality} image generation"
        ))

        connection.commit()

        return new_balance

    except HTTPException:
        if connection:
            connection.rollback()
        raise

    except Exception as error:
        if connection:
            connection.rollback()

        print(
            "CREDIT RESERVATION ERROR:",
            repr(error),
            flush=True
        )

        raise HTTPException(
            status_code=503,
            detail="Could not verify your credits."
        )

    finally:
        if connection:
            connection.close()
def refund_credits(user_id, amount, quality):
    connection = None

    try:
        connection = psycopg2.connect(
            os.getenv("DATABASE_URL")
        )

        cursor = connection.cursor()

        cursor.execute("""
            UPDATE app_users
            SET credits = credits + %s
            WHERE user_id = %s
            RETURNING credits;
        """, (amount, user_id))

        result = cursor.fetchone()

        if not result:
            raise Exception("Account not found during refund.")

        cursor.execute("""
            INSERT INTO credit_transactions
            (user_id, amount, transaction_type, description)
            VALUES (%s, %s, %s, %s);
        """, (
            user_id,
            amount,
            "generation_refund",
            f"Failed {quality} image generation"
        ))

        connection.commit()

        return result[0]

    except Exception as error:
        if connection:
            connection.rollback()

        print(
            "CREDIT REFUND ERROR:",
            repr(error),
            flush=True
        )

        raise

    finally:
        if connection:
            connection.close()
def record_generation(user_id, quality, provider):
    connection = None

    try:
        connection = psycopg2.connect(
            os.getenv("DATABASE_URL")
        )

        cursor = connection.cursor()

        cursor.execute("""
            INSERT INTO generation_history
            (user_id, quality, provider, status)
            VALUES (%s, %s, %s, %s);
        """, (
            user_id,
            quality,
            provider,
            "success"
        ))

        connection.commit()
        cursor.close()

    except Exception as error:
        if connection:
            connection.rollback()

        print(
            "GENERATION HISTORY ERROR:",
            repr(error),
            flush=True
        )

    finally:
        if connection:
            connection.close()



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
def generate_image(
    request: ImageRequest,
    current_user: dict = Depends(get_authenticated_user)
):
    request_started = time.perf_counter()
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

        try:
            if not api_key:
                raise HTTPException(
                status_code=500,
                detail="OpenAI API key is missing."
            )
            
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
                record_generation(
    current_user["user_id"],
    quality,
    "openai"
                )

            return {
                "image": image_data,
                "provider": "openai",
                "quality": "high",
                "status": "success"
            }

        except HTTPException:
    refund_credits(
        current_user["user_id"],
        credits_used,
        quality
    )
    raise

except Exception as error:

    refund_credits(
        current_user["user_id"],
        credits_used,
        quality
    )

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

    

    try:
        if not api_token or not account_id:
            raise HTTPException(
            status_code=500,
            detail="Cloudflare credentials are missing."
        )
        cloudflare_started = time.perf_counter()
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
        cloudflare_seconds = (
            time.perf_counter() - cloudflare_started
        )

        print(
            f"CLOUDFLARE GENERATION: {cloudflare_seconds:.2f} seconds",
            flush=True
        )

        result = response.json()

        if not result.get("success"):
            raise HTTPException(
                status_code=502,
                detail="Cloudflare image generation failed."
            )

        image_data = result["result"]["image"]
        total_seconds = time.perf_counter() - request_started

        print(
            f"TOTAL BACKEND TIME: {total_seconds:.2f} seconds",
            flush=True
        )
        record_generation(
    current_user["user_id"],
    quality,
    "cloudflare"
)

        return {
            "image": image_data,
            "provider": "cloudflare",
            "quality": quality,
            "status": "success"
        }

        except HTTPException:
        refund_credits(
            current_user["user_id"],
            credits_used,
            quality
        )
        raise

    except Exception as error:

        refund_credits(
            current_user["user_id"],
            credits_used,
            quality
        )

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
    image: UploadFile = File(...),
    current_user: dict = Depends(get_authenticated_user)
):
    prompt = prompt.strip()
    quality = quality.lower().strip()

    if quality not in ["low", "medium", "high"]:
        raise HTTPException(
            status_code=400,
            detail="Quality must be low, medium, or high."
        )
    credits_used = CREDIT_COSTS[quality]

new_balance = reserve_credits(
    current_user["user_id"],
    credits_used,
    quality
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

    try:
        original = Image.open(io.BytesIO(image_data))
        original = original.convert("RGB")

        # HIGH QUALITY: OpenAI image editing
        if quality == "high":

            openai_key = os.getenv("OPENAI_API_KEY")

            if not openai_key:
                raise HTTPException(
                    status_code=500,
                    detail="OpenAI API key is missing."
                )

            openai_buffer = io.BytesIO()
            original.save(
                openai_buffer,
                format="JPEG",
                quality=95
            )
            openai_buffer.seek(0)

            client = OpenAI(api_key=openai_key)

            result = client.images.edit(
                model="gpt-image-2.5-sunburst",
                image=(
                    "uploaded.jpg",
                    openai_buffer,
                    "image/jpeg"
                ),
                prompt=prompt,
                quality="high"
            )

            if not result.data or not result.data[0].b64_json:
                raise HTTPException(
                    status_code=502,
                    detail="OpenAI returned no edited image."
                )

            generated_image = result.data[0].b64_json

        else:
            # LOW and MEDIUM: Existing Cloudflare editing
            api_token = os.getenv("CLOUDFLARE_API_TOKEN")
            account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")

            if not api_token or not account_id:
                raise HTTPException(
                    status_code=500,
                    detail="Cloudflare credentials are missing."
                )

            original.thumbnail((511, 511))

            width = max(256, (original.width // 8) * 8)
            height = max(256, (original.height // 8) * 8)

            original = original.resize((width, height))

            image_buffer = io.BytesIO()
            original.save(
                image_buffer,
                format="JPEG",
                quality=90
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

            if response.headers.get("content-type", "").startswith("image/"):
                generated_image = base64.b64encode(
                    response.content
                ).decode("utf-8")

            else:
                result = response.json()

                if not result.get("success"):
                    raise HTTPException(
                        status_code=502,
                        detail=str(
                            result.get("errors", "Image editing failed.")
                        )
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

# CREATE CREDIT SYSTEM TABLES

@app.post("/setup-database")
def setup_database():

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise HTTPException(
            status_code=500,
            detail="DATABASE_URL is missing."
        )

    try:
        connection = psycopg2.connect(database_url)
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_users (
                id BIGSERIAL PRIMARY KEY,
                user_id UUID UNIQUE NOT NULL,
                credits INTEGER NOT NULL DEFAULT 0
                    CHECK (credits >= 0),
                created_at TIMESTAMPTZ
                    DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS credit_transactions (
                id BIGSERIAL PRIMARY KEY,
                user_id UUID NOT NULL
                    REFERENCES app_users(user_id),
                amount INTEGER NOT NULL,
                transaction_type VARCHAR(30) NOT NULL,
                description TEXT,
                created_at TIMESTAMPTZ
                    DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS generation_history (
                id BIGSERIAL PRIMARY KEY,
                user_id UUID NOT NULL
                    REFERENCES app_users(user_id),
                quality VARCHAR(20) NOT NULL,
                provider VARCHAR(30),
                status VARCHAR(20) NOT NULL,
                created_at TIMESTAMPTZ
                    DEFAULT CURRENT_TIMESTAMP
            );
        """)

        connection.commit()
        cursor.close()
        connection.close()

        return {
            "status": "success",
            "message": "Credit system tables created."
        }

    except Exception as error:
        print(
            "DATABASE SETUP ERROR:",
            repr(error),
            flush=True
        )

        raise HTTPException(
            status_code=500,
            detail="Database setup failed."
        )
# ANONYMOUS USER REGISTRATION

@app.post("/register")
def register_user():

    user_id = str(uuid.uuid4())

    access_token = secrets.token_urlsafe(32)

    token_hash = hashlib.sha256(
        access_token.encode()
    ).hexdigest()

    try:
        connection = psycopg2.connect(
            os.getenv("DATABASE_URL")
        )

        cursor = connection.cursor()

        cursor.execute("""
            ALTER TABLE app_users
            ADD COLUMN IF NOT EXISTS token_hash TEXT UNIQUE;
        """)

        cursor.execute("""
            INSERT INTO app_users (
                user_id,
                credits,
                token_hash
            )
            VALUES (%s, 0, %s);
        """, (user_id, token_hash))

        connection.commit()

        cursor.close()
        connection.close()

        return {
            "status": "success",
            "user_id": user_id,
            "access_token": access_token,
            "credits": 0
        }

    except Exception as error:

        print(
            "REGISTRATION ERROR:",
            repr(error),
            flush=True
        )

        raise HTTPException(
            status_code=500,
            detail="Account registration failed."
        )
        
        
