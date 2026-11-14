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
import cloudinary
import cloudinary.uploader
from fastapi import UploadFile, File, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from decimal import Decimal
from fastapi import Request
from fastapi import Depends
from openai import OpenAI
from PIL import Image
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field
from pwdlib import PasswordHash
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from pydantic import EmailStr
from pydantic import BaseModel
password_hash = PasswordHash.recommended()


app = FastAPI(title="Thomas AI Backend")
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)
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
class EmailRegisterRequest(BaseModel):
    name: str
    email: str
    password: str


class EmailLoginRequest(BaseModel):
    email: str
    password: str


class GoogleLoginRequest(BaseModel):
    credential: str
    
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

def create_auth_session(cursor, user_id, credits):
    access_token = secrets.token_urlsafe(32)

    token_hash = hashlib.sha256(
        access_token.encode()
    ).hexdigest()

    cursor.execute("""
        UPDATE app_users
        SET token_hash = %s
        WHERE user_id = %s;
    """, (token_hash, user_id))

    return {
        "status": "success",
        "user_id": str(user_id),
        "access_token": access_token,
        "credits": credits
    }


@app.post("/auth/register")
def auth_register(data: EmailRegisterRequest):
    name = data.name.strip()
    email = data.email.strip().lower()
    password = data.password

    if not name or "@" not in email:
        raise HTTPException(400, "Enter a valid name and email.")

    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")

    connection = None

    try:
        connection = psycopg2.connect(os.getenv("DATABASE_URL"))
        cursor = connection.cursor()

        cursor.execute("""
            SELECT user_id FROM app_users
            WHERE LOWER(email) = %s;
        """, (email,))

        if cursor.fetchone():
            raise HTTPException(409, "Email already registered.")

        user_id = str(uuid.uuid4())
        access_token = secrets.token_urlsafe(32)

        token_hash = hashlib.sha256(
            access_token.encode()
        ).hexdigest()

        hashed_password = password_hash.hash(password)

        cursor.execute("""
            INSERT INTO app_users (
                user_id, credits, token_hash,
                name, email, password_hash
            )
            VALUES (%s, 0, %s, %s, %s, %s);
        """, (
            user_id,
            token_hash,
            name,
            email,
            hashed_password
        ))

        connection.commit()
        cursor.close()

        return {
            "status": "success",
            "user_id": user_id,
            "access_token": access_token,
            "credits": 0
        }

    except HTTPException:
        if connection:
            connection.rollback()
        raise

    except psycopg2.errors.UniqueViolation:
        if connection:
            connection.rollback()
        raise HTTPException(409, "Email already registered.")

    except Exception as error:
        if connection:
            connection.rollback()

        print("AUTH REGISTER ERROR:", repr(error), flush=True)
        raise HTTPException(500, "Registration failed.")

    finally:
        if connection:
            connection.close()


@app.post("/auth/login")
def auth_login(data: EmailLoginRequest):
    email = data.email.strip().lower()

    connection = None

    try:
        connection = psycopg2.connect(os.getenv("DATABASE_URL"))
        cursor = connection.cursor()

        cursor.execute("""
            SELECT user_id, credits, password_hash
            FROM app_users
            WHERE LOWER(email) = %s;
        """, (email,))

        user = cursor.fetchone()

        if (
            not user
            or not user[2]
            or not password_hash.verify(data.password, user[2])
        ):
            raise HTTPException(401, "Invalid email or password.")

        result = create_auth_session(
            cursor, user[0], user[1]
        )

        connection.commit()
        cursor.close()

        return result

    except HTTPException:
        if connection:
            connection.rollback()
        raise

    except Exception as error:
        if connection:
            connection.rollback()

        print("AUTH LOGIN ERROR:", repr(error), flush=True)
        raise HTTPException(500, "Login failed.")

    finally:
        if connection:
            connection.close()


@app.post("/auth/google")
def auth_google(data: GoogleLoginRequest):
    connection = None

    try:
        client_id = os.getenv("GOOGLE_CLIENT_ID")

        if not client_id:
            raise HTTPException(500, "Google login is not configured.")

        google_user = id_token.verify_oauth2_token(
            data.credential,
            google_requests.Request(),
            client_id
        )

        email = google_user.get("email", "").strip().lower()
        google_sub = google_user.get("sub")
        name = google_user.get("name", "Thomas AI User")

        if not email or not google_sub or not google_user.get("email_verified"):
            raise HTTPException(401, "Google account verification failed.")

        connection = psycopg2.connect(os.getenv("DATABASE_URL"))
        cursor = connection.cursor()

        cursor.execute("""
            SELECT user_id, credits, google_sub
            FROM app_users
            WHERE LOWER(email) = %s
            FOR UPDATE;
        """, (email,))

        user = cursor.fetchone()

        if user:
            if user[2] != google_sub:
                raise HTTPException(
                     409,
                     "An account with this email already exists. Log in first and link Google from account settings."
             )

            user_id = user[0]
            credits = user[1]

            cursor.execute("""
                UPDATE app_users
                SET google_sub = %s
                WHERE user_id = %s;
            """, (google_sub, user[0]))

            user_id = user[0]
            credits = user[1]

        else:
            user_id = str(uuid.uuid4())
            credits = 0

            cursor.execute("""
                INSERT INTO app_users (
                    user_id, credits, name, email, google_sub
                )
                VALUES (%s, 0, %s, %s, %s);
            """, (user_id, name, email, google_sub))

        result = create_auth_session(
            cursor, user_id, credits
        )

        connection.commit()
        cursor.close()

        return result

    except HTTPException:
        if connection:
            connection.rollback()
        raise

    except ValueError:
        if connection:
            connection.rollback()
        raise HTTPException(401, "Invalid Google identity token.")

    except Exception as error:
        if connection:
            connection.rollback()

        print("GOOGLE AUTH ERROR:", repr(error), flush=True)
        raise HTTPException(500, "Google login failed.")

    finally:
        if connection:
            connection.close()


@app.post("/auth/link-email")
def link_email(
    data: EmailRegisterRequest,
    current_user: dict = Depends(get_authenticated_user)
):
    name = data.name.strip()
    email = data.email.strip().lower()

    if not name or "@" not in email:
        raise HTTPException(400, "Enter a valid name and email.")

    if len(data.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")

    connection = None

    try:
        connection = psycopg2.connect(os.getenv("DATABASE_URL"))
        cursor = connection.cursor()

        cursor.execute("""
            UPDATE app_users
            SET name = %s,
                email = %s,
                password_hash = %s
            WHERE user_id = %s
              AND email IS NULL;
        """, (
            name,
            email,
            password_hash.hash(data.password),
            current_user["user_id"]
        ))

        if cursor.rowcount == 0:
            raise HTTPException(
                409,
                "Account already has an email or does not exist."
            )

        connection.commit()
        cursor.close()

        return {"status": "success", "message": "Account linked successfully."}

    except HTTPException:
        if connection:
            connection.rollback()
        raise

    except psycopg2.errors.UniqueViolation:
        if connection:
            connection.rollback()
        raise HTTPException(409, "Email already belongs to another account.")

    except Exception as error:
        if connection:
            connection.rollback()

        print("ACCOUNT LINK ERROR:", repr(error), flush=True)
        raise HTTPException(500, "Account linking failed.")

    finally:
        if connection:
            connection.close()

@app.get("/auth/status")
def get_account_status(
    current_user: dict = Depends(get_authenticated_user)
):
    connection = None
    cursor = None

    try:
        connection = psycopg2.connect(os.getenv("DATABASE_URL"))
        cursor = connection.cursor()

        cursor.execute("""
            SELECT name, email, credits
            FROM app_users
            WHERE user_id = %s;
        """, (current_user["user_id"],))

        account = cursor.fetchone()

        if account is None:
            raise HTTPException(404, "Account not found.")

        name, email, credits = account

        return {
            "status": "success",
            "name": name,
            "email": email,
            "has_email": email is not None,
            "credits": credits
        }

    except HTTPException:
        raise

    except Exception as error:
        print("ACCOUNT STATUS ERROR:", repr(error), flush=True)
        raise HTTPException(500, "Unable to retrieve account status.")

    finally:
        if cursor:
            cursor.close()
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





def record_generation(
    user_id,
    quality,
    provider,
    reservation_id,
    model,
    provider_cost_usd=None
):
    connection = None

    try:
        connection = psycopg2.connect(
            os.getenv("DATABASE_URL")
        )
        cursor = connection.cursor()

        print(
            "GENERATION TRACKING:",
            user_id,
            quality,
            provider,
            reservation_id,
            model,
            flush=True
        )

        cursor.execute("""
            INSERT INTO generation_history (
                user_id,
                quality,
                provider,
                status,
                reservation_id,
                model,
                provider_cost_usd
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s);
        """, (
            user_id,
            quality,
            provider,
            "success",
            reservation_id,
            model,
            provider_cost_usd
        ))

        if provider_cost_usd is not None:
            cost = Decimal(str(provider_cost_usd))

            if cost > 0:
                cost_minor = int(
                    (cost * Decimal("100000000")).quantize(
                        Decimal("1")
                    )
                )

                description = f"{provider} usage: {model}"

                # Ensure the provider account exists.
                cursor.execute("""
                    INSERT INTO provider_accounts (
                        provider,
                        currency,
                        balance_minor,
                        unit_scale
                    )
                    VALUES (%s, 'USD', 0, 100000000)
                    ON CONFLICT (provider)
                    DO NOTHING;
                """, (provider,))

                # Record usage only once.
                cursor.execute("""
                    INSERT INTO provider_transactions (
                        provider,
                        transaction_type,
                        amount_minor,
                        currency,
                        unit_scale,
                        source_reference,
                        description
                    )
                    VALUES (
                        %s,
                        'usage',
                        %s,
                        'USD',
                        100000000,
                        %s,
                        %s
                    )
                    ON CONFLICT (
                        provider,
                        transaction_type,
                        source_reference
                    )
                    WHERE source_reference IS NOT NULL
                    DO NOTHING
                    RETURNING id;
                """, (
                    provider,
                    cost_minor,
                    str(reservation_id),
                    description
                ))

                usage_inserted = cursor.fetchone() is not None

                # Update balance only for a new usage transaction.
                if usage_inserted:

                    cursor.execute("""
                        UPDATE provider_accounts
                        SET
                            balance_minor = balance_minor - %s,
                            updated_at = NOW()
                        WHERE provider = %s
                          AND currency = 'USD';
                    """, (
                        cost_minor,
                        provider
                    ))

                    # Record the expense in the business ledger.
                    cursor.execute("""
                        INSERT INTO business_ledger (
                            entry_type,
                            direction,
                            amount_minor,
                            currency,
                            unit_scale,
                            source,
                            source_reference,
                            description,
                            metadata
                        )
                        VALUES (
                            'provider_usage',
                            'expense',
                            %s,
                            'USD',
                            100000000,
                            'provider',
                            %s,
                            %s,
                            jsonb_build_object(
                                'provider', %s,
                                'model', %s,
                                'user_id', %s
                            )
                        )
                        ON CONFLICT (
                            source,
                            source_reference,
                            entry_type
                        )
                        DO NOTHING;
                    """, (
                        cost_minor,
                        str(reservation_id),
                        description,
                        provider,
                        model,
                        str(user_id)
                    ))

        connection.commit()

        print(
            "GENERATION ACCOUNTING SUCCESSFUL",
            flush=True
        )

        cursor.close()

    except Exception as error:
        if connection:
            connection.rollback()

        print(
            "GENERATION ACCOUNTING ERROR:",
            repr(error),
            flush=True
        )

    finally:
        if connection:
            connection.close()



def record_provider_topup(
    provider,
    amount_usd,
    source_reference,
    description=None
):
    connection = None

    allowed_providers = {
        "cloudflare",
        "openai",
        "higgsfield",
        "luma"
    }

    if provider not in allowed_providers:
        raise ValueError("Unsupported provider")

    amount = Decimal(str(amount_usd))

    if amount <= 0:
        raise ValueError("Top-up amount must be positive")

    if not source_reference:
        raise ValueError("A unique top-up reference is required")

    amount_minor = int(
        (amount * Decimal("100000000")).quantize(
            Decimal("1")
        )
    )

    if amount_minor <= 0:
        raise ValueError("Top-up amount is too small")

    connection = psycopg2.connect(
        os.getenv("DATABASE_URL")
    )

    try:
        cursor = connection.cursor()

        description = description or (
            f"{provider} provider top-up"
        )

        cursor.execute("""
            INSERT INTO provider_accounts (
                provider,
                currency,
                balance_minor,
                unit_scale
            )
            VALUES (%s, 'USD', 0, 100000000)
            ON CONFLICT (provider)
            DO NOTHING;
        """, (provider,))

        cursor.execute("""
            INSERT INTO provider_transactions (
                provider,
                transaction_type,
                amount_minor,
                currency,
                unit_scale,
                source_reference,
                description
            )
            VALUES (
                %s,
                'topup',
                %s,
                'USD',
                100000000,
                %s,
                %s
            )
            ON CONFLICT (
                provider,
                transaction_type,
                source_reference
            )
            WHERE source_reference IS NOT NULL
            DO NOTHING
            RETURNING id;
        """, (
            provider,
            amount_minor,
            source_reference,
            description
        ))

        inserted = cursor.fetchone()

        if inserted is None:
            connection.rollback()
            print("TOP-UP ALREADY RECORDED", flush=True)
            return False

        cursor.execute("""
            UPDATE provider_accounts
            SET
                balance_minor = balance_minor + %s,
                updated_at = NOW()
            WHERE provider = %s
              AND currency = 'USD';
        """, (
            amount_minor,
            provider
        ))

        cursor.execute("""
            INSERT INTO business_ledger (
                entry_type,
                direction,
                amount_minor,
                currency,
                unit_scale,
                source,
                source_reference,
                description,
                metadata
            )
            VALUES (
                'provider_topup',
                'transfer',
                %s,
                'USD',
                100000000,
                'provider',
                %s,
                %s,
                jsonb_build_object(
                    'provider', %s
                )
            )
            ON CONFLICT (
                source,
                source_reference,
                entry_type
            )
            DO NOTHING;
        """, (
            amount_minor,
            source_reference,
            description,
            provider
        ))

        connection.commit()

        print(
            "PROVIDER TOP-UP RECORDED:",
            provider,
            amount_usd,
            flush=True
        )

        return True

    except Exception as error:
        connection.rollback()

        print(
            "PROVIDER TOP-UP ERROR:",
            repr(error),
            flush=True
        )

        raise

    finally:
        connection.close()
    
                    
        
                

        
            
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
                "user_id": str(current_user["user_id"]),
                "package": package_name
                
                }
            },
            timeout=30
                )

        
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
        cursor.execute("""
    INSERT INTO business_ledger (
        entry_type,
        direction,
        amount_minor,
        currency,
        unit_scale,
        source,
        source_reference,
        description,
        metadata
    )
    VALUES (
        'customer_payment',
        'income',
        %s,
        'NGN',
        100,
        'paystack',
        %s,
        %s,
        jsonb_build_object(
            'user_id', %s,
            'credits', %s
        )
    );
""", (
    expected_amount,
    reference,
    f"Paystack credit purchase: {credits} credits",
    str(user_id),
    credits
))

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

class ImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)
    quality: str = "low"

def get_provider_cost(provider, model, feature):
    connection = None

    try:
        connection = psycopg2.connect(
            os.getenv("DATABASE_URL")
        )
        cursor = connection.cursor()

        cursor.execute("""
            SELECT cost_per_unit
            FROM provider_pricing
            WHERE provider = %s
              AND model = %s
              AND feature = %s
        """, (provider, model, feature))

        result = cursor.fetchone()

        if not result:
            raise ValueError("Provider pricing not configured.")

        return float(result[0])

    finally:
        if connection:
            connection.close()
            

def get_cloudflare_generation_cost(model, steps, tiles):
    connection = psycopg2.connect(
        os.getenv("DATABASE_URL")
    )

    try:
        cursor = connection.cursor()

        cursor.execute("""
            SELECT feature, cost_per_unit, currency
            FROM provider_pricing
            WHERE provider = 'cloudflare'
              AND model = %s
              AND feature IN (
                  'image_generation_step',
                  'image_generation_tile'
              );
        """, (model,))

        rows = cursor.fetchall()

        pricing = {
            row[0]: (Decimal(str(row[1])), row[2])
            for row in rows
        }

        step_price = pricing.get("image_generation_step")
        tile_price = pricing.get("image_generation_tile")

        if not step_price or not tile_price:
            raise ValueError("Cloudflare pricing is incomplete")

        if step_price[1] != "USD" or tile_price[1] != "USD":
            raise ValueError("Unexpected pricing currency")

        total = (
            tiles * step_price[0] +
            steps * tile_price[0]
        )

        return float(total)

    finally:
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
    "openai",
    reservation_id,
    "gpt-image-2.5-flare"
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
                "prompt": prompt,
                "steps": 4
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
        cloudflare_steps = 4
        cloudflare_tiles = 4

        cloudflare_cost_usd = (
            cloudflare_tiles * 0.0000528 +
            cloudflare_steps * 0.0001056
        )

        total_seconds = time.perf_counter() - request_started

        print(
            f"TOTAL BACKEND TIME: {total_seconds:.2f} seconds",
            flush=True
        )


        record_generation(
    current_user["user_id"],
    quality,
    "cloudflare",
    reservation_id,
    "@cf/black-forest-labs/flux-1-schnell",
    cloudflare_cost_usd
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
    user_id = current_user["user_id"]

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
    user_id,
    quality,
    provider,
    reservation_id,
    model
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
            ALTER TABLE app_users
            ADD COLUMN IF NOT EXISTS name TEXT;
        """)

        cursor.execute("""
            ALTER TABLE app_users
            ADD COLUMN IF NOT EXISTS email TEXT;
        """)

        cursor.execute("""
            ALTER TABLE app_users
            ADD COLUMN IF NOT EXISTS password_hash TEXT;
        """)

        cursor.execute("""
            ALTER TABLE app_users
            ADD COLUMN IF NOT EXISTS google_sub TEXT;
        """)

        cursor.execute("""
            ALTER TABLE app_users
            ADD COLUMN IF NOT EXISTS token_hash TEXT UNIQUE;
         """)

        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            app_users_email_unique
            ON app_users (LOWER(email))
            WHERE email IS NOT NULL;
        """)

        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            app_users_google_sub_unique
            ON app_users (google_sub)
            WHERE google_sub IS NOT NULL;
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

# =====================================
# PIXVERSE VIDEO GENERATION
# =====================================

@app.post("/generate-video")
def generate_video(data: dict):

    import os
    import json
    import uuid
    import urllib.request
    import urllib.error

    api_key = os.getenv("PIXVERSE_API_KEY")

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="Video service is not configured."
        )

    prompt = data.get("prompt", "").strip()

    duration = data.get("duration", 5)
    quality = data.get("quality", "720p")
    model = data.get("model", "v6")
    aspect_ratio = data.get("aspect_ratio", "16:9")

    if not prompt:
        raise HTTPException(
            status_code=400,
            detail="Prompt is required."
        )

    if len(prompt) > 5000:
        raise HTTPException(
            status_code=400,
            detail="Prompt is too long."
        )

    if duration not in range(1, 16):
        raise HTTPException(
            status_code=400,
            detail="Duration must be between 1 and 15 seconds."
        )

    if quality not in ["360p", "540p", "720p", "1080p"]:
        raise HTTPException(
            status_code=400,
            detail="Invalid video quality."
        )

    if model not in [
        "v6-max", "c1", "v6", "v3.5", "v4",
        "v4.5", "v5", "v5-fast", "v5.5", "v5.6"
    ]:
        raise HTTPException(
            status_code=400,
            detail="Invalid video model."
        )

    if aspect_ratio not in [
        "16:9", "9:16", "4:3", "3:4",
        "1:1", "2:3", "3:2", "21:9"
    ]:
        raise HTTPException(
            status_code=400,
            detail="Invalid aspect ratio."
        )

    payload = {
        "model": model,
        "prompt": prompt,
        "duration": duration,
        "quality": quality,
        "aspect_ratio": aspect_ratio
    }

    request = urllib.request.Request(
        "https://app-api.pixverse.ai/openapi/v2/video/text/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "API-KEY": api_key,
            "Ai-trace-id": str(uuid.uuid4()),
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=60
        ) as response:

            result = json.loads(
                response.read().decode("utf-8")
            )

        if result.get("ErrCode") != 0:
            raise HTTPException(
                status_code=502,
                detail=result.get(
                    "ErrMsg",
                    "PixVerse rejected the request."
                )
            )

        video_id = result.get("Resp", {}).get("video_id")

        if not video_id:
            raise HTTPException(
                status_code=502,
                detail="PixVerse did not return a video ID."
            )

        return {
            "status": "processing",
            "video_id": video_id,
            "credits": result.get("Resp", {}).get("credits")
        }

    except HTTPException:
        raise

    except urllib.error.HTTPError as error:

        print(
            "PIXVERSE GENERATION ERROR:",
            error.code,
            error.read().decode("utf-8", errors="replace"),
            flush=True
        )

        raise HTTPException(
            status_code=502,
            detail="PixVerse video generation request failed."
        )

    except Exception as error:

        print(
            "VIDEO GENERATION ERROR:",
            repr(error),
            flush=True
        )

        raise HTTPException(
            status_code=502,
            detail="Could not connect to video generation service."
        )

@app.get("/video-status/{video_id}")
def get_video_status(video_id: str):

    import os
    import json
    import uuid
    import urllib.request
    import urllib.error

    api_key = os.getenv("PIXVERSE_API_KEY")

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="Video service is not configured."
        )

    if not video_id or len(video_id) > 150:
        raise HTTPException(
            status_code=400,
            detail="Invalid video ID."
        )

    trace_id = str(uuid.uuid4())

    url = (
        "https://app-api.pixverse.ai/openapi/v2/video/result/"
        + urllib.parse.quote(video_id, safe="")
    )

    request = urllib.request.Request(
        url,
        headers={
            "API-KEY": api_key,
            "Ai-trace-id": trace_id,
            "Accept-Language": "en"
        },
        method="GET"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            result = json.loads(
                response.read().decode("utf-8")
            )

        if result.get("ErrCode") != 0:
            raise HTTPException(
                status_code=502,
                detail=result.get(
                    "ErrMsg",
                    "Could not retrieve video status."
                )
            )

        return result

    except HTTPException:
        raise

    except urllib.error.HTTPError as error:

        print(
            "PIXVERSE STATUS HTTP ERROR:",
            error.code,
            error.read().decode("utf-8", errors="replace"),
            flush=True
        )

        raise HTTPException(
            status_code=502,
            detail="PixVerse status request failed."
        )

    except urllib.error.URLError as error:

        print(
            "PIXVERSE STATUS CONNECTION ERROR:",
            repr(error.reason),
            flush=True
        )

        raise HTTPException(
            status_code=502,
            detail="Could not connect to PixVerse."
        )

    except Exception as error:

        print(
            "VIDEO STATUS ERROR:",
            repr(error),
            flush=True
        )

        raise HTTPException(
            status_code=502,
            detail="Could not retrieve video status."
        )
        


# =====================================
# CHECK VIDEO GENERATION STATUS
# =====================================


@app.get("/video-templates")
def get_video_templates(
    type: str = "1",
    page: int = 1,
    pageSize: int = 20,
    template_mode: str = None
):
    import os
    import json
    import uuid
    import urllib.request
    import urllib.error
    import urllib.parse

    api_key = os.getenv("PIXVERSE_API_KEY")

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="Video service is not configured."
        )

    if type not in ("1", "2"):
        raise HTTPException(
            status_code=400,
            detail="Invalid template type."
        )

    if page < 1 or pageSize not in (10, 20, 50, 100):
        raise HTTPException(
            status_code=400,
            detail="Invalid page or pageSize."
        )

    params = {
        "type": type,
        "page": page,
        "pageSize": pageSize
    }

    if template_mode:
        params["template_mode"] = template_mode

    query_string = urllib.parse.urlencode(params)

    url = (
        "https://app-api.pixverse.ai"
        "/openapi/v2/video/effects/templates/list?"
        + query_string
    )

    request = urllib.request.Request(
        url,
        headers={
            "API-KEY": api_key,
            "Ai-trace-id": str(uuid.uuid4()),
            "Accept-Language": "en"
        },
        method="GET"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            result = json.loads(
                response.read().decode("utf-8")
            )

        if result.get("ErrCode") != 0:
            print(
                "PIXVERSE TEMPLATE API ERROR:",
                result.get("ErrCode"),
                result.get("ErrMsg"),
                flush=True
            )

            raise HTTPException(
                status_code=502,
                detail="PixVerse could not retrieve templates."
            )

        data = result.get("Resp") or {}

        return {
            "templates": data.get("effect_items", []),
            "page": page,
            "pageSize": pageSize
        }

    except HTTPException:
        raise

    except urllib.error.HTTPError as error:
        error_body = error.read().decode(
            "utf-8",
            errors="replace"
        )

        print(
            "PIXVERSE TEMPLATE HTTP ERROR:",
            error.code,
            error_body,
            flush=True
        )

        raise HTTPException(
            status_code=502,
            detail=f"PixVerse returned HTTP {error.code}."
        )

    except urllib.error.URLError as error:
        print(
            "PIXVERSE CONNECTION ERROR:",
            repr(error.reason),
            flush=True
        )

        raise HTTPException(
            status_code=502,
            detail="Could not connect to PixVerse."
        )

    except Exception as error:
        print(
            "PIXVERSE TEMPLATE ERROR:",
            repr(error),
            flush=True
        )

        raise HTTPException(
            status_code=502,
            detail="PixVerse template request failed."
    )
        


@app.get("/video-templates")
def get_video_templates():

    import os
    import json
    import uuid
    import urllib.request
    import urllib.error

    api_key = os.getenv("PIXVERSE_API_KEY")

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="Video service is not configured."
        )

    trace_id = str(uuid.uuid4())

    print("PIXVERSE REQUEST TIME:", __import__("datetime").datetime.now().isoformat(), flush=True)
    print("PIXVERSE ENDPOINT:", url, flush=True)
    print("PIXVERSE TRACE ID:", trace_id, flush=True)

    request = urllib.request.Request(
       url,
       headers={
           "API-KEY": api_key,
            "Ai-trace-id": trace_id,
          "Accept-Language": "en"
       },
        method="GET"
  )
    try:
        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            result = json.loads(
                response.read().decode("utf-8")
            )

        if result.get("ErrCode") != 0:
            raise HTTPException(
                status_code=502,
                detail=result.get(
                    "ErrMsg",
                    "Could not retrieve video templates."
                )
            )

        data = result.get("Resp") or {}

        return {
            "templates": data.get("effect_items", [])
        }

    except HTTPException:
        raise

    except urllib.error.HTTPError as error:

        print(
            "PIXVERSE TEMPLATE ERROR:",
            error.code,
            error.read().decode("utf-8", errors="replace"),
            flush=True
        )

        raise HTTPException(
            status_code=502,
            detail="Could not retrieve PixVerse templates."
        )

    except Exception as error:

        print(
            "TEMPLATE ERROR:",
            repr(error),
            flush=True
        )

        error_body = error.read().decode(
       "utf-8",
        errors="replace"
)

        print(
            "PIXVERSE TEMPLATE ERROR:",
             error.code,
             error_body,
             flush=True
)

        raise HTTPException(
             status_code=502,
             detail="PixVerse template request failed."
        )
@app.post("/upload-template-image")
async def upload_template_image(file: UploadFile = File(...)):

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail="Please upload an image."
        )

    try:
        result = cloudinary.uploader.upload(
            file.file,
            folder="thomas_ai/template_uploads",
            resource_type="image"
        )

        return {
            "secure_url": result.get("secure_url"),
            "public_id": result.get("public_id")
        }

    except Exception as error:
        print(
            "CLOUDINARY TEMPLATE UPLOAD ERROR:",
            repr(error),
            flush=True
        )

        raise HTTPException(
            status_code=502,
            detail="Could not upload image."
        )

    finally:
        await file.close()


@app.get("/higgsfield-auth-check")
def higgsfield_auth_check():
    import os
    import requests

    key = os.getenv("HF_API_KEY")

    if not key:
        return {"error": "HF_API_KEY is missing"}

    response = requests.get(
        "https://api.higgsfield.ai/models/higgsfield/genjutsu/restyle/v1.0/presets",
        headers={
            "Authorization": f"Key {key}",
            "Accept": "application/json"
        },
        timeout=30
    )

    return {
        "status_code": response.status_code,
        "authenticated": response.status_code == 200,
        "result": response.text[:1500]
    }


from pydantic import BaseModel, Field
from typing import Literal

class MotionTransferRequest(BaseModel):
    video_url: str
    image_urls: list[str] = Field(min_length=1, max_length=8)
    prompt: str = ""
    resolution: Literal["480p", "720p", "1080p"] = "720p"
    duration_seconds: int = Field(ge=1, le=5)


@app.post("/motion-transfer")
def motion_transfer(data: MotionTransferRequest):
    import os
    import requests

    key = os.getenv("HF_API_KEY")
    if not key:
        raise HTTPException(
           status_code=500,
           detail="HF_API_KEY is missing"
       )

    try:
        response = requests.post(
            "https://api.higgsfield.ai/higgsfield/genjutsu/motion-transfer/v1.0",
            headers={
                "Authorization": f"Key {key}",
                "Content-Type": "application/json"
            },
            json=data.model_dump(),
            timeout=30
        )

        try:
            result = response.json()
        except ValueError:
            result = {"detail": response.text}

        return {
            "status_code": response.status_code,
            "result": result
        }

    except requests.RequestException as e:
        raise HTTPException(
          status_code=502,
          detail=f"Higgsfield connection failed: {str(e)}"
        )
        

@app.get("/motion-transfer-status/{request_id}")
def motion_transfer_status(request_id: str):
    import os
    import requests

    key = os.getenv("HF_API_KEY")

    if not key:
        raise HTTPException(
            status_code=500,
            detail="HF_API_KEY is missing"
        )

    try:
        response = requests.get(
            f"https://api.higgsfield.ai/requests/{request_id}/status",
            headers={"Authorization": f"Key {key}"},
            timeout=30
        )

        if response.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Higgsfield status error: {response.text[:1000]}"
            )

        return response.json()

    except requests.RequestException as e:
        raise HTTPException(
            status_code=502,
            detail=f"Status connection failed: {str(e)}"
        )

    except ValueError:
        raise HTTPException(
            status_code=502,
            detail="Invalid JSON from Higgsfield"
        )


@app.api_route("/", methods=["GET", "HEAD"])
def health_check():
    return {"status": "ok"}

@app.post("/upload-template-video")
async def upload_template_video(file: UploadFile = File(...)):
    import os
    import cloudinary
    import cloudinary.uploader

    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(
            status_code=400,
            detail="Please upload a video file"
        )

    try:
        cloudinary.config(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
            api_key=os.getenv("CLOUDINARY_API_KEY"),
            api_secret=os.getenv("CLOUDINARY_API_SECRET")
        )

        result = cloudinary.uploader.upload(
            file.file,
            resource_type="video",
            folder="thomas_ai/motion_transfer"
        )

        return {
            "success": True,
            "video_url": result["secure_url"],
            "public_id": result["public_id"],
            "duration": result.get("duration")
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )
    finally:
        await file.close()
        


@app.get("/admin/financial-summary")
def financial_summary(
    current_user: dict = Depends(get_authenticated_user)
):
    connection = None

    try:
        connection = psycopg2.connect(
            os.getenv("DATABASE_URL")
        )
        cursor = connection.cursor()

        # Verify admin access
        cursor.execute("""
            SELECT is_admin
            FROM app_users
            WHERE user_id = %s;
        """, (current_user["user_id"],))

        admin = cursor.fetchone()

        if not admin or not admin[0]:
            raise HTTPException(
                status_code=403,
                detail="Admin access required."
            )

        cursor.execute("""
            SELECT
                l.currency,
                l.entry_type,
                l.direction,
                COALESCE(SUM(l.amount_minor), 0),
                MAX(r.rate_to_usd),
                MAX(l.unit_scale)
            FROM business_ledger l
            LEFT JOIN currency_rates r
                ON r.currency = l.currency
            GROUP BY
                l.currency,
                l.entry_type,
                l.direction
            ORDER BY l.currency, l.entry_type;
        """)

        rows = cursor.fetchall()

        totals = {}
        revenue_usd = 0.0
        expenses_usd = 0.0
        missing_rates = []

        for currency, entry_type, direction, amount, rate, scale in rows:
            if not rate:
                missing_rates.append(currency)
                continue

            if currency not in totals:
                totals[currency] = {
                    "revenue": 0,
                    "expenses": 0,
                    "refunds": 0
                }

            value = int(amount)

            if entry_type == "customer_payment":
                totals[currency]["revenue"] += value
                revenue_usd += (value / scale) * float(rate)

            elif direction == "expense":
                totals[currency]["expenses"] += value
                expenses_usd += (value / scale) * float(rate)

            elif entry_type == "refund":
                totals[currency]["refunds"] += value

        return {
            "reporting_currency": "USD",
            "currency_totals_minor": totals,
            "revenue_usd": round(revenue_usd, 8),
            "expenses_usd": round(expenses_usd, 8),
            "net_profit_usd": round(
                revenue_usd - expenses_usd, 8
            ),
            "missing_exchange_rates": list(set(missing_rates))
        }

    except HTTPException:
        raise

    except Exception as error:
        print("FINANCIAL SUMMARY ERROR:", repr(error))
        raise HTTPException(
            status_code=500,
            detail="Could not generate financial summary."
        )

    finally:
        if connection:
            connection.close()
            
class ProviderTopupRequest(BaseModel):
    provider: str
    amount_usd: Decimal = Field(gt=0)
    source_reference: str
    description: str | None = None
    
@app.post("/admin/provider-topup")
def admin_provider_topup(
    request: ProviderTopupRequest,
    current_user: dict = Depends(get_authenticated_user)
):
    connection = None

    try:
        connection = psycopg2.connect(
            os.getenv("DATABASE_URL")
        )
        cursor = connection.cursor()

        # Verify admin access
        cursor.execute("""
            SELECT is_admin
            FROM app_users
            WHERE user_id = %s;
        """, (current_user["user_id"],))

        admin = cursor.fetchone()

        if not admin or not admin[0]:
            raise HTTPException(
                status_code=403,
                detail="Admin access required."
            )

        cursor.close()
        connection.close()
        connection = None

        if not request.source_reference.strip():
            raise HTTPException(
                status_code=400,
                detail="Funding reference is required."
            )

        try:
            recorded = record_provider_topup(
                provider=request.provider,
                amount_usd=request.amount_usd,
                source_reference=request.source_reference.strip(),
                description=request.description
            )

        except ValueError as error:
            raise HTTPException(
                status_code=400,
                detail=str(error)
            )

        if not recorded:
            return {
                "status": "duplicate",
                "message": "This provider top-up was already recorded."
            }

        return {
            "status": "success",
            "provider": request.provider,
            "amount_usd": str(request.amount_usd),
            "message": "Provider top-up recorded successfully."
        }

    except HTTPException:
        raise

    except Exception as error:
        print(
            "PROVIDER TOP-UP ENDPOINT ERROR:",
            repr(error),
            flush=True
        )

        raise HTTPException(
            status_code=500,
            detail="Could not record provider top-up."
        )

    finally:
        if connection:
            connection.close()
    
