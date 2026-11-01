import os
from datetime import timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-key-change-me")
    DATABASE = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "instance", "tracker.db"))
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", os.path.join(BASE_DIR, "uploads"))
    THUMBNAIL_FOLDER = os.environ.get("THUMBNAIL_FOLDER", os.path.join(BASE_DIR, "thumbnails"))
    REFERENCE_UPLOAD_FOLDER = os.environ.get("REFERENCE_UPLOAD_FOLDER", os.path.join(BASE_DIR, "reference_uploads"))
    REFERENCE_THUMBNAIL_FOLDER = os.environ.get("REFERENCE_THUMBNAIL_FOLDER", os.path.join(BASE_DIR, "reference_thumbnails"))
    MAX_CONTENT_LENGTH = 1024 * 1024 * 1024  # 1 GB total upload size per request - large batches of high-res ComfyUI PNGs add up fast
    # Password hash for the single-user login. Generate with generate_password.py.
    APP_PASSWORD_HASH = os.environ.get("APP_PASSWORD_HASH", "")
    # Static key for non-browser clients (e.g. HTTP Shortcuts) that can't
    # carry a session cookie - sent as the X-API-Key header. Only the
    # share-target upload route accepts this; everything else still
    # requires a real login.
    API_KEY = os.environ.get("API_KEY", "")
    ALLOWED_EXTENSIONS = {"png"}  # ComfyUI metadata only survives in PNG
    REFERENCE_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
    # A single-user personal tool doesn't need short session timeouts, and a
    # long-lived session matters more here than usual: Android can kill and
    # restart the installed PWA's process between uses (e.g. to handle a
    # Share Target intent), and a plain non-permanent session cookie doesn't
    # reliably survive that - silently logging Tim out right when a share
    # is trying to land, dropping the shared file with no way to recover it.
    PERMANENT_SESSION_LIFETIME = timedelta(days=365)
