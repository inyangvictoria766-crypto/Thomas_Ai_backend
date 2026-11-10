import os
import io
import requests
import psycopg2
import base64
import time
import uuid
import secrets
import hmac
import hashlib
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from fastapi import Request
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
CREDIT_PACKAGES = {
    "starter": {
        "amount": 50000,
        "credits": 20
    },
    "standard": {
        "amount": 100000,
        "credits": 50
    },
    "premium": {
        "amount": 250000,
        "credits": 150
    }
}

class CreditPurchaseRequest(BaseModel):
    package: str
    email: str
    
from fastapi import Header

security = HTTPBearer()

def get_authenticated_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    access_token = credentials.credentials

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
    reservation_id = str(uuid.uuid4())

    try:
        connection = psycopg2.connect(
            os.getenv("DATABASE_URL")
        )

        cursor = connection.cursor()

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
            (
                user_id,
                amount,
                transaction_type,
                description,
                reservation_id
            )
            VALUES (%s, %s, %s, %s, %s::uuid);
        """, (
            user_id,
            -amount,
            "generation_reservation",
            f"{quality} image generation",
            reservation_id
        ))

        connection.commit()

        return new_balance, reservation_id

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
def refund_credits(user_id, reservation_id, quality):
    connection = None

    try:
        connection = psycopg2.connect(
            os.getenv("DATABASE_URL")
        )

        cursor = connection.cursor()

        cursor.execute("""
            INSERT INTO credit_transactions
            (
                user_id,
                amount,
                transaction_type,
                description,
                reservation_id
            )
            SELECT
                user_id,
                -amount,
                'generation_refund',
                %s,
                reservation_id
            FROM credit_transactions
            WHERE user_id = %s
              AND reservation_id = %s
              AND transaction_type = 'generation_reservation'
            ON CONFLICT (reservation_id, transaction_type)
            WHERE reservation_id IS NOT NULL
            DO NOTHING
            RETURNING amount;
        """, (
            f"Failed {quality} image generation",
            user_id,
            reservation_id
        ))

        result = cursor.fetchone()

        if not result:
            connection.commit()
            print(
                "REFUND SKIPPED: Already refunded or reservation not found.",
                flush=True
            )
            return False

        refund_amount = result[0]

        cursor.execute("""
            UPDATE app_users
            SET credits = credits + %s
            WHERE user_id = %s
            RETURNING credits;
        """, (refund_amount, user_id))

        balance = cursor.fetchone()

        if not balance:
            raise Exception("Account not found during refund.")

        connection.commit()
        return balance[0]

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


@app.get("/credits")
def get_credits(
    current_user: dict = Depends(get_authenticated_user)
):
    return {
        "status": "success",
        "user_id": current_user["user_id"],
        "credits": current_user["credits"]
}
    
# buying credits 
@app.post("/buy-credits")
def buy_credits(
    data: CreditPurchaseRequest,
    current_user: dict = Depends(get_authenticated_user)
):
    package_name = data.package.lower().strip()
    package = CREDIT_PACKAGES.get(package_name)

    if not package:
        raise HTTPException(
            status_code=400,
            detail="Invalid credit package."
        )

    secret_key = os.getenv("PAYSTACK_SECRET_KEY")

    if not secret_key:
        raise HTTPException(
            status_code=503,
            detail="Payment service is not configured."
        )

    reference = "thomas_" + uuid.uuid4().hex

    connection = None

    try:
        connection = psycopg2.connect(
            os.getenv("DATABASE_URL")
        )
        cursor = connection.cursor()

        cursor.execute("""
            INSERT INTO payment_transactions
            (user_id, reference, amount, credits, status)
            VALUES (%s, %s, %s, %s, 'pending');
        """, (
            current_user["user_id"],
            reference,
            package["amount"],
            package["credits"]
        ))

        connection.commit()
        cursor.close()

    except Exception:
        if connection:
            connection.rollback()

        raise HTTPException(
            status_code=500,
            detail="Could not create payment record."
        )

    finally:
        if connection:
            connection.close()

    try:
        response = requests.post(
            "https://api.paystack.co/transaction/initialize",
            headers={
                "Authorization": f"Bearer {secret_key}",
                "Content-Type": "application/json"
            },
            json={
                "email": data.email,
                "amount": package["amount"],
                "currency": "NGN",
                "reference": reference,
                "metadata": {
                    "user_id": current_user["user_id"],
                    "package": package_name
                }
            },
            timeout=30
                )

        t
        result = response.json()
        payment_data = result.get("data") or {}

        authorization_url = payment_data.get("authorization_url")
        returned_reference = payment_data.get("reference")

        if (
             not response.ok
             or not result.get("status")
             or not authorization_url
            or not authorization_url.startswith(
                "https://checkout.paystack.com/"
            )
            or returned_reference != reference
        ):
            raise HTTPException(
                status_code=502,
                detail="Could not initialize payment."
            )

        
        return {
            "status": "success",
            "authorization_url": authorization_url,
            "reference": reference
        }

    except requests.RequestException:
        raise HTTPException(
            status_code=502,
            detail="Payment service temporarily unavailable."
        )

        

    

@app.get("/payment-status/{reference}")
def payment_status(
    reference: str,
    current_user: dict = Depends(get_authenticated_user)
):
    connection = None

    try:
        connection = psycopg2.connect(
            os.getenv("DATABASE_URL")
        )
        cursor = connection.cursor()

        cursor.execute("""
            SELECT status, credits
            FROM payment_transactions
            WHERE reference = %s
            AND user_id = %s;
        """, (
            reference,
            current_user["user_id"]
        ))

        transaction = cursor.fetchone()

        if not transaction:
            raise HTTPException(
                status_code=404,
                detail="Payment not found."
            )

        return {
            "status": transaction[0],
            "credits": transaction[1]
        }

    finally:
        if connection:
            connection.close()
@app.post("/paystack-webhook")
async def paystack_webhook(request: Request):

    secret_key = os.getenv("PAYSTACK_SECRET_KEY")

    if not secret_key:
        raise HTTPException(
            status_code=503,
            detail="Payment service unavailable."
        )

    raw_body = await request.body()

    signature = request.headers.get(
        "x-paystack-signature", ""
    )

    expected_signature = hmac.new(
        secret_key.encode(),
        raw_body,
        hashlib.sha512
    ).hexdigest()

    if not hmac.compare_digest(
        signature, expected_signature
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid payment signature."
        )

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid webhook data."
        )

    if payload.get("event") != "charge.success":
        return {"status": "ignored"}

    payment = payload.get("data") or {}
    reference = payment.get("reference")
    amount = payment.get("amount")
    currency = payment.get("currency")
    metadata = payment.get("metadata") or {}

    if not reference or currency != "NGN":
        raise HTTPException(
            status_code=400,
            detail="Invalid payment details."
        )
    try:
        verification_response = requests.get(
            f"https://api.paystack.co/transaction/verify/{reference}",
            headers={
                "Authorization": f"Bearer {secret_key}"
            },
            timeout=20
        )

        verification = verification_response.json()
        verified_payment = verification.get("data") or {}
        verified_metadata = verified_payment.get("metadata") or {}

        if (
            not verification_response.ok
            or not verification.get("status")
            or verified_payment.get("status") != "success"
            or verified_payment.get("reference") != reference
            or verified_payment.get("amount") != amount
            or verified_payment.get("currency") != "NGN"
        ):
            raise HTTPException(
                status_code=400,
                detail="Paystack payment verification failed."
            )

    except requests.RequestException:
        raise HTTPException(
            status_code=502,
            detail="Could not verify payment with Paystack."
        )

    connection = None

    try:
        connection = psycopg2.connect(
            os.getenv("DATABASE_URL")
        )
        cursor = connection.cursor()

        cursor.execute("""
            SELECT user_id, amount, credits, status
            FROM payment_transactions
            WHERE reference = %s
            FOR UPDATE;
        """, (reference,))

        transaction = cursor.fetchone()

        if not transaction:
            raise HTTPException(
                status_code=404,
                detail="Payment reference not found."
            )

        user_id, expected_amount, credits, status = transaction

        if (
            amount != expected_amount
            or verified_metadata.get("user_id") != str(user_id)
        ):
            raise HTTPException(
                status_code=400,
                detail="Payment verification failed."
            )

        if status == "successful":
            connection.commit()
            return {"status": "already_processed"}

        if status != "pending":
            raise HTTPException(
                status_code=400,
                detail="Payment is not pending."
            )

        cursor.execute("""
            UPDATE app_users
            SET credits = credits + %s
            WHERE user_id = %s;
        """, (credits, user_id))

        cursor.execute("""
            UPDATE payment_transactions
            SET status = 'successful',
                paid_at = NOW()
            WHERE reference = %s;
        """, (reference,))

        connection.commit()

        return {
            "status": "success",
            "credits_added": credits
        }

    except HTTPException:
        if connection:
            connection.rollback()
        raise

    except Exception as error:
        if connection:
            connection.rollback()

        print("PAYSTACK WEBHOOK ERROR:", repr(error), flush=True)

        raise HTTPException(
            status_code=500,
            detail="Payment processing failed."
        )

    finally:
        if connection:
            connection.close()
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
     # RESERVE CREDITS
    credits_used = CREDIT_COSTS[quality]

    new_balance, reservation_id = reserve_credits(
        current_user["user_id"],
        credits_used,
        quality
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
                reservation_id,
                quality
            )
            raise

        except Exception as error:
            refund_credits(
                current_user["user_id"],
                reservation_id,
                quality
            )
            print("OPENAI IMAGE ERROR:", repr(error), flush=True)
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
            reservation_id,
            quality
        )
        raise

    except Exception as error:
        refund_credits(
            current_user["user_id"],
            reservation_id,
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

    # VALIDATE PROMPT
    if not prompt:
        raise HTTPException(
            status_code=400,
            detail="Please enter an editing prompt."
        )

    # VALIDATE QUALITY
    if quality not in ["low", "medium", "high"]:
        raise HTTPException(
            status_code=400,
            detail="Quality must be low, medium, or high."
        )

    # VALIDATE UPLOAD TYPE
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

    # RESERVE CREDITS
    credits_used = CREDIT_COSTS[quality]

    
    new_balance, reservation_id = reserve_credits(
        current_user["user_id"],
        credits_used,
        quality
    )

    try:
        # OPEN UPLOADED IMAGE
        original = Image.open(io.BytesIO(image_data))
        original = original.convert("RGB")

        generated_image = None
        provider = None

        # HIGH QUALITY: OPENAI
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

            client = OpenAI(
                api_key=openai_key,
                timeout=180.0,
                max_retries=1
            )

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
            provider = "openai"

        # LOW AND MEDIUM: CLOUDFLARE
        else:

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

            provider = "cloudflare"

            if response.headers.get(
                "content-type", ""
            ).startswith("image/"):

                generated_image = base64.b64encode(
                    response.content
                ).decode("utf-8")

            else:
                result = response.json()

                if not result.get("success"):
                    raise HTTPException(
                        status_code=502,
                        detail=str(
                            result.get(
                                "errors",
                                "Image editing failed."
                            )
                        )
                    )

                output = result.get("result")

                if isinstance(output, dict):
                    generated_image = output.get("image")

                elif isinstance(output, str):
                    generated_image = output

        # VALIDATE GENERATED IMAGE
        if not generated_image:
            raise HTTPException(
                status_code=502,
                detail="The model returned no image."
            )

        # SAVE GENERATION HISTORY
        record_generation(
            current_user["user_id"],
            quality,
            provider
        )

        return {
            "image": generated_image,
            "status": "success",
            "message": "Image edited successfully!"
        }

    except HTTPException:
        refund_credits(
            current_user["user_id"],
            reservation_id,
            quality
        )
        raise

    except Exception as error:
        refund_credits(
            current_user["user_id"],
            reservation_id,
            quality
        )

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
        
        
