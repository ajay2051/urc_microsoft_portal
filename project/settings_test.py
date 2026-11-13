import os

from tests.keys import API_CLIENT_ID, JWKS_JSON, TENANT_ID

os.environ.setdefault("DJANGO_DEBUG", "true")

from project.settings import *  # noqa: E402,F401,F403

DEBUG = True
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
ENTRA_TENANT_ID = TENANT_ID
ENTRA_API_CLIENT_ID = API_CLIENT_ID
ENTRA_JWKS_JSON = JWKS_JSON
ENTRA_ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
ENTRA_JWKS_URL = f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"
ENTRA_REQUIRED_SCOPE = "access_as_user"
GRAPH_BACKEND = "mock"
TRUST_X_FORWARDED_FOR = False