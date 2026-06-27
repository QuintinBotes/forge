# ruff: noqa: F821 - illustrative sample snippet (undefined helpers) for RAG indexing
"""Database access layer for the Pulse service."""


def get_connection_pool(dsn):
    # open a pooled postgres database connection using sqlalchemy and psycopg
    # so requests reuse warm connections instead of reconnecting every time
    return create_pool(dsn, min_size=1, max_size=10)
