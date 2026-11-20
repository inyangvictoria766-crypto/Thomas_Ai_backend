
import os
import uuid
import psycopg2
from decimal import Decimal, ROUND_HALF_UP

import thomas_ai


DATABASE_URL = os.getenv("NEON_TEST_DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("Test database secret is missing.")

os.environ["DATABASE_URL"] = DATABASE_URL


def to_minor(cost):
    return int(
        (Decimal(str(cost)) * Decimal("100000000")).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP
        )
    )


def run_tests():
    provider = "settle_" + uuid.uuid4().hex[:12]
    reservation_id = str(uuid.uuid4())
    missing_usage_id = str(uuid.uuid4())

    cost = Decimal("0.00123456")
    cost_minor = to_minor(cost)

    connection = psycopg2.connect(DATABASE_URL)

    try:
        with connection:
            with connection.cursor() as cursor:

                # Reservation with matching usage transaction.
                cursor.execute("""
                    INSERT INTO provider_cost_reservations (
                        reservation_id,
                        provider,
                        estimated_cost_minor,
                        status
                    )
                    VALUES (%s, %s, %s, 'reserved')
                """, (reservation_id, provider, 200000))

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
                        %s, 'usage', %s, 'USD',
                        100000000, %s, 'Settlement test'
                    )
                """, (provider, cost_minor, reservation_id))

                # Reservation without a usage transaction.
                cursor.execute("""
                    INSERT INTO provider_cost_reservations (
                        reservation_id,
                        provider,
                        estimated_cost_minor,
                        status
                    )
                    VALUES (%s, %s, %s, 'reserved')
                """, (missing_usage_id, provider, 200000))

    finally:
        connection.close()

    # 1. Successful settlement.
    assert thomas_ai.settle_provider_budget(
        reservation_id, cost
    ) is True

    # 2. Duplicate settlement must be rejected.
    assert thomas_ai.settle_provider_budget(
        reservation_id, cost
    ) is False

    # 3. Missing usage transaction must be rejected.
    assert thomas_ai.settle_provider_budget(
        missing_usage_id, cost
    ) is False

    # 4. Negative cost must be rejected.
    try:
        thomas_ai.settle_provider_budget(
            reservation_id, Decimal("-0.01")
        )
        raise AssertionError("Negative cost was accepted.")
    except ValueError:
        pass

    # 5. Verify final database state.
    verify = psycopg2.connect(DATABASE_URL)

    try:
        with verify.cursor() as cursor:

            cursor.execute("""
                SELECT status, actual_cost_minor
                FROM provider_cost_reservations
                WHERE reservation_id = %s
            """, (reservation_id,))

            assert cursor.fetchone() == ("settled", cost_minor)

            cursor.execute("""
                SELECT status, actual_cost_minor
                FROM provider_cost_reservations
                WHERE reservation_id = %s
            """, (missing_usage_id,))

            assert cursor.fetchone() == ("reserved", None)

    finally:
        verify.close()

    print("ALL PROVIDER SETTLEMENT TESTS PASSED!")


if __name__ == "__main__":
    run_tests()
  
