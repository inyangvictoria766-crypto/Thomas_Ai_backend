import os
import uuid
import psycopg2

# The test database URL will be supplied privately by GitHub Actions.
DATABASE_URL = os.getenv("NEON_TEST_DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("NEON_TEST_DATABASE_URL is missing.")

print("Test environment is ready.")
