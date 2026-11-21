
import os
import uuid
import psycopg2
import thomas_ai

DATABASE_URL = os.getenv("NEON_TEST_DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("NEON_TEST_DATABASE_URL is missing.")

os.environ["DATABASE_URL"] = DATABASE_URL


def run_tests():
    user_id = str(uuid.uuid4())
    reservation_id = str(uuid.uuid4())

    connection = psycopg2.connect(DATABASE_URL)

    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO app_users (user_id, credits)
                    VALUES (%s, 8)
                """, (user_id,))

                cursor.execute("""
                    INSERT INTO credit_transactions
                    (
                        user_id,
                        amount,
                        transaction_type,
                        description,
                        reservation_id
                    )
                    VALUES (%s, -2, 'generation_reservation',
                            'Refund test', %s)
                """, (user_id, reservation_id))
    finally:
        connection.close()

    # First refund must succeed.
    first = thomas_ai.refund_credits(
        user_id, reservation_id, "low"
    )

    assert first == 10, f"Unexpected first balance: {first}"

    # Second refund must be rejected.
    second = thomas_ai.refund_credits(
        user_id, reservation_id, "low"
    )

    assert second is False, "Duplicate refund was accepted."

    check = psycopg2.connect(DATABASE_URL)

    try:
        with check.cursor() as cursor:
            cursor.execute("""
                SELECT credits
                FROM app_users
                WHERE user_id = %s
            """, (user_id,))

            balance = cursor.fetchone()[0]

            cursor.execute("""
                SELECT COUNT(*)
                FROM credit_transactions
                WHERE reservation_id = %s
                  AND transaction_type = 'generation_refund'
            """, (reservation_id,))

            refunds = cursor.fetchone()[0]

            assert balance == 10
            assert refunds == 1

        print("ALL REFUND TESTS PASSED!")
    finally:
        check.close()


if __name__ == "__main__":
    run_tests()
  
