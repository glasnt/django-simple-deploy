{{current_settings}}


# --- Settings for Cloud Run ---
import os

if os.environ.get("ON_CLOUDRUN"):
    # Static file configuration needs to take effect during the build process,
    #   and when deployed.
    # from https://whitenoise.evans.io/en/stable/#quickstart-for-django-apps
    STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")
    STATIC_URL = "/static/"
    STATICFILES_DIRS = (os.path.join(BASE_DIR, "static"),)
    i = MIDDLEWARE.index("django.middleware.security.SecurityMiddleware")
    MIDDLEWARE.insert(i + 1, "whitenoise.middleware.WhiteNoiseMiddleware")

    # Use secret, if set, to update DEBUG value.
    if os.environ.get("DEBUG") == "FALSE":
        DEBUG = False
    elif os.environ.get("DEBUG") == "TRUE":
        DEBUG = True

    # Set a Cloud Run-specific allowed host.
    ALLOWED_HOSTS.append("{{ deployed_url }}")

    # Prevent CSRF "Origin checking failed" issue.
    CSRF_TRUSTED_ORIGINS = ["https://{{ deployed_url }}"]

    # Use the Cloud SQL database.
    import dj_database_url

    db_url = os.environ.get("DATABASE_URL")
    DATABASES["default"] = dj_database_url.parse(db_url)

    # TODO(glasnt): Remove when jazzband/dj-database-url#181 included in release (1.1.0+)
    import urllib
    DATABASES["default"]["HOST"] = urllib.parse.unquote(DATABASES["default"]["HOST"])
