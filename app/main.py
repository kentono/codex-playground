import os
import sqlite3
import uuid
import warnings
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.datastructures import UploadFile

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_IMAGE_BYTES + 64 * 1024
Image.MAX_IMAGE_PIXELS = 20_000_000
BASE = Path(__file__).parent


@contextmanager
def connect():
    connection = sqlite3.connect(DATA_DIR / "items.db", timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


@asynccontextmanager
async def lifespan(app):
    (DATA_DIR / "uploads").mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, name TEXT NOT NULL, image_path TEXT NOT NULL, created_at TEXT NOT NULL)")
    yield


class LimitRequestBody:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        # Buffer only a bounded body, including requests without Content-Length.
        chunks, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > MAX_REQUEST_BYTES:
                from starlette.responses import PlainTextResponse
                return await PlainTextResponse("Upload too large. Choose an image under 10 MB.", status_code=413)(scope, receive, send)
            chunks.append(body)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


app = FastAPI(lifespan=lifespan)
app.add_middleware(LimitRequestBody)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.mount("/uploads", StaticFiles(directory=DATA_DIR / "uploads", check_dir=False), name="uploads")
templates = Jinja2Templates(directory=BASE / "templates")


@app.get("/health")
def health():
    with connect() as db:
        db.execute("SELECT 1 FROM items LIMIT 1")
    return {"status": "ok"}


@app.get("/")
def items(request: Request):
    with connect() as db:
        rows = db.execute("SELECT * FROM items ORDER BY created_at DESC, id DESC").fetchall()
    return templates.TemplateResponse(request=request, name="items.html", context={"items": rows})


@app.get("/items/new")
def new_item(request: Request):
    return render_form(request)


def render_form(request, error=None, name="", status_code=200):
    return templates.TemplateResponse(request=request, name="new.html", context={"error": error, "name": name}, status_code=status_code)


@app.post("/items")
async def create_item(request: Request):
    async with request.form(max_files=1, max_fields=2) as form:
        raw_name = form.get("name", "")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        upload = form.get("image")
        if not name or len(name) > 120:
            return render_form(request, "Enter a name between 1 and 120 characters.", name, 400)
        if not isinstance(upload, UploadFile):
            return render_form(request, "Choose an image to add your item.", name, 400)
        content = await upload.read(MAX_IMAGE_BYTES + 1)
        if len(content) > MAX_IMAGE_BYTES:
            return render_form(request, "Choose an image under 10 MB.", name, 400)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(content)) as source:
                    if source.format not in {"JPEG", "PNG", "WEBP"}:
                        raise ValueError("Unsupported format")
                    source.load()
                    picture = ImageOps.exif_transpose(source).convert("RGB")
                    picture.thumbnail((1200, 1200))
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            return render_form(request, "Choose a valid JPEG, PNG, or WebP image (up to 20 million pixels).", name, 400)
    item_id = uuid.uuid4().hex
    filename = f"{item_id}.jpg"
    path = DATA_DIR / "uploads" / filename
    try:
        picture.save(path, "JPEG", quality=85)
        with connect() as db:
            db.execute("INSERT INTO items VALUES (?, ?, ?, ?)", (item_id, name, filename, datetime.now(timezone.utc).isoformat()))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return RedirectResponse("/", status_code=303)
