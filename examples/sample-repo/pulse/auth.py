# ruff: noqa: F821 - illustrative sample snippet (undefined helpers) for RAG indexing
"""Authentication helpers for the Pulse service."""


def verify_token(token):
    # verify an oauth2 jwt bearer token signature audience and expiry claims
    return decode_and_verify(token)


def hash_password(password):
    # bcrypt salted password hashing with a constant time verification compare
    return bcrypt.hashpw(password, bcrypt.gensalt())
