import os
import uuid
import psycopg2
from decimal import Decimal, ROUND_HALF_UP


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
        from decimal import Decimal, ROUND_HALF_UP

        if provider_cost_usd is None:
            print(
                "ACCOUNTING PENDING: Provider cost unavailable.",
                flush=True
            )
            return False

        cost = Decimal(str(provider_cost_usd))

        if not cost.is_finite() or cost <= 0:
            print(
                "ACCOUNTING PENDING: Invalid or zero provider cost.",
                flush=True
            )
            return False

        cost_minor = int(
            (cost * Decimal("100000000")).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP
            )
        )

        if cost_minor <= 0:
            raise ValueError("Provider cost is below accounting precision.")

        description = f"{provider} usage: {model}"

        connection = psycopg2.connect(
            os.getenv("DATABASE_URL")
        )

        with connection:
            with connection.cursor() as cursor:

                cursor.execute("""
                    INSERT INTO generation_history (
                        user_id,
                        quality,
                        provider,
                        status,
                        reservation_id,
                        model,
                        provider_cost_usd,
                        accounting_status
                    )
                    VALUES (
                        %s, %s, %s, 'success',
                        %s, %s, %s, 'pending'
                    )
                    ON CONFLICT (reservation_id)
                    WHERE reservation_id IS NOT NULL
                    DO NOTHING;
                """, (
                    user_id,
                    quality,
                    provider,
                    reservation_id,
                    model,
                    cost,
                ))

                cursor.execute("""
                    SELECT
                        user_id,
                        provider,
                        model,
                        provider_cost_usd,
                        accounting_status
                    FROM generation_history
                    WHERE reservation_id = %s
                    FOR UPDATE;
                """, (reservation_id,))

                history = cursor.fetchone()

                if not history:
                    raise ValueError(
                        "Generation history record not found."
                    )

                if (
                    str(history[0]) != str(user_id)
                    or history[1] != provider
                    or history[2] != model
                    or Decimal(str(history[3])) != cost
                ):
                    raise ValueError(
                        "Existing generation record does not match."
                    )

                cursor.execute("""
                    SELECT provider, estimated_cost_minor, actual_cost_minor, status
                    FROM provider_cost_reservations
                    WHERE reservation_id = %s
                    FOR UPDATE;
                """, (reservation_id,))

                reservation = cursor.fetchone()

                if not reservation or reservation[0] != provider:
                    raise ValueError(
                        "Matching provider reservation not found."
                    )

                if reservation[3] == "released":
                    raise ValueError(
                        "Provider reservation was already released."
                    )

                if reservation[3] == "settled":
                    if reservation[2] != cost_minor:
                        raise ValueError(
                            "Settled reservation cost does not match."
                        )

                    cursor.execute("""
                        UPDATE generation_history
                        SET accounting_status = 'recorded'
                        WHERE reservation_id = %s;
                    """, (reservation_id,))

                    print(
                        "GENERATION ACCOUNTING ALREADY SETTLED",
                        flush=True
                    )
                    return True

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
                        %s, 'usage', %s, 'USD',
                        100000000, %s, %s
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

                if not usage_inserted:
                    cursor.execute("""
                        SELECT provider, amount_minor, currency
                        FROM provider_transactions
                        WHERE provider = %s
                          AND transaction_type = 'usage'
                          AND source_reference = %s;
                    """, (provider, str(reservation_id)))

                    existing_usage = cursor.fetchone()

                    if (
                        not existing_usage
                        or existing_usage[0] != provider
                        or existing_usage[1] != cost_minor
                        or existing_usage[2] != "USD"
                    ):
                        raise ValueError(
                            "Existing provider usage transaction does not match."
                        )

                if usage_inserted:

                    cursor.execute("""
                        UPDATE provider_accounts
                        SET
                            balance_minor = balance_minor - %s,
                            updated_at = NOW()
                        WHERE provider = %s
                          AND currency = 'USD';
                    """, (cost_minor, provider))

                    cursor.execute("""
                        INSERT INTO provider_funding (
                            provider,
                            currency,
                            funded_amount_minor,
                            spent_amount_minor,
                            low_balance_threshold_minor
                        )
                        VALUES (
                            %s, 'USD', 0, %s, 100000000
                        )
                        ON CONFLICT (provider, currency)
                        DO UPDATE SET
                            spent_amount_minor =
                                provider_funding.spent_amount_minor
                                + EXCLUDED.spent_amount_minor,
                            updated_at = NOW();
                    """, (provider, cost_minor))

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

                cursor.execute("""
                    UPDATE provider_cost_reservations
                    SET
                        actual_cost_minor = %s,
                        status = 'settled',
                        updated_at = NOW()
                    WHERE reservation_id = %s
                      AND provider = %s
                      AND status = 'reserved'
                    RETURNING id;
                """, (
                    cost_minor,
                    reservation_id,
                    provider
                ))

                if cursor.fetchone() is None:
                    raise ValueError(
                        "Provider reservation could not be settled."
                    )

                cursor.execute("""
                    UPDATE generation_history
                    SET accounting_status = 'recorded'
                    WHERE reservation_id = %s;
                """, (reservation_id,))

                print(
                    "GENERATION ACCOUNTING RECORDED",
                    flush=True
                )

                return True

    except Exception as error:
        print(
            "GENERATION ACCOUNTING ERROR:",
            repr(error),
            flush=True
        )
        return False

    finally:
        if connection is not None:
            connection.close()


# TEST HARNESS
DATABASE_URL = os.getenv("NEON_TEST_DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("Test database secret is missing.")

os.environ["DATABASE_URL"] = DATABASE_URL

test_connection = psycopg2.connect(DATABASE_URL)

try:
    with test_connection:
        with test_connection.cursor() as cursor:
            cursor.execute(
                "SELECT user_id FROM app_users LIMIT 1"
            )
            user = cursor.fetchone()

            if not user:
                raise RuntimeError("No test user found.")

            user_id = user[0]
            provider = "test_" + uuid.uuid4().hex[:12]
            reservation_id = str(uuid.uuid4())
            cost = Decimal("0.00123456")

            cost_minor = int(
                (cost * Decimal("100000000")).quantize(
                    Decimal("1"),
                    rounding=ROUND_HALF_UP
                )
            )

            cursor.execute("""
                INSERT INTO provider_cost_reservations (
                    reservation_id,
                    provider,
                    estimated_cost_minor,
                    status
                )
                VALUES (%s, %s, %s, 'reserved')
            """, (reservation_id, provider, 200000))

    test_connection.close()

    # First call: should record the usage.
    first = record_generation(
        user_id,
        "test",
        provider,
        reservation_id,
        "test-model",
        cost
    )

    # Second call: should not record a duplicate.
    second = record_generation(
        user_id,
        "test",
        provider,
        reservation_id,
        "test-model",
        cost
    )

    if not first or not second:
        raise RuntimeError("record_generation returned False.")

    check = psycopg2.connect(DATABASE_URL)

    try:
        with check.cursor() as cursor:
            cursor.execute("""
                SELECT balance_minor
                FROM provider_accounts
                WHERE provider = %s
            """, (provider,))
            balance = cursor.fetchone()[0]

            cursor.execute("""
                SELECT spent_amount_minor
                FROM provider_funding
                WHERE provider = %s AND currency = 'USD'
            """, (provider,))
            spent = cursor.fetchone()[0]

            cursor.execute("""
                SELECT COUNT(*)
                FROM provider_transactions
                WHERE provider = %s
                  AND transaction_type = 'usage'
                  AND source_reference = %s
            """, (provider, reservation_id))
            transactions = cursor.fetchone()[0]

            cursor.execute("""
                SELECT COUNT(*)
                FROM business_ledger
                WHERE source = 'provider'
                  AND source_reference = %s
                  AND entry_type = 'provider_usage'
            """, (reservation_id,))
            ledger = cursor.fetchone()[0]

            cursor.execute("""
                SELECT status, actual_cost_minor
                FROM provider_cost_reservations
                WHERE reservation_id = %s
            """, (reservation_id,))
            reservation = cursor.fetchone()

            assert balance == -cost_minor
            assert spent == cost_minor
            assert transactions == 1
            assert ledger == 1
            assert reservation == ("settled", cost_minor)

            print("ALL ACCOUNTING TESTS PASSED!")

    finally:
        check.close()

finally:
    if not test_connection.closed:
        test_connection.close()
    
