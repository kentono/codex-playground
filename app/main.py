import csv
import os
import re
import sqlite3
import uuid
import warnings
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
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
        columns = {row["name"] for row in db.execute("PRAGMA table_info(items)")}
        for column, kind in (("item_date", "TEXT"), ("price_cents", "INTEGER"), ("description", "TEXT")):
            if column not in columns:
                db.execute(f"ALTER TABLE items ADD COLUMN {column} {kind}")
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


@app.get("/items/export.csv")
def export_items():
    with connect() as db:
        rows = db.execute("SELECT * FROM items ORDER BY created_at DESC, id DESC").fetchall()
    output = StringIO(newline="")
    columns = ["id", "name", "image_path", "created_at", "item_date", "price_cents", "description"]
    writer = csv.DictWriter(output, fieldnames=columns + ["price"], quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for row in rows:
        record = dict(row)
        cents = record["price_cents"]
        record["price"] = f"{cents // 100}.{cents % 100:02d}" if cents is not None else ""
        writer.writerow(record)
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="itemkeeper-items.csv"', "Cache-Control": "no-store"},
    )


@app.get("/items/new")
def new_item(request: Request):
    return render_form(request)


def get_item(item_id):
    with connect() as db:
        item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@app.get("/items/{item_id}/edit")
def edit_item(request: Request, item_id: str):
    item = get_item(item_id)
    cents = item["price_cents"]
    fields = {"item_date": item["item_date"] or "", "description": item["description"] or "",
              "price": f"{cents // 100}.{cents % 100:02d}" if cents is not None else ""}
    return render_form(request, name=item["name"], fields=fields, item=item)


def render_form(request, error=None, name="", status_code=200, fields=None, item=None):
    context = {"error": error, "name": name, "item_date": "", "price": "", "description": ""}
    context.update(fields or {})
    context["item"] = item
    return templates.TemplateResponse(request=request, name="new.html", context=context, status_code=status_code)


@app.post("/items")
async def create_item(request: Request):
    return await save_item(request)


@app.post("/items/{item_id}/edit")
async def update_item(request: Request, item_id: str):
    return await save_item(request, get_item(item_id))


async def save_item(request: Request, item=None):
    async with request.form(max_files=1, max_fields=4) as form:
        raw_name = form.get("name", "")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        fields = {key: form.get(key, "").strip() if isinstance(form.get(key, ""), str) else ""
                  for key in ("item_date", "price", "description")}

        def invalid(message):
            return render_form(request, message, name, 400, fields, item)

        item_date = fields["item_date"] or None
        if item_date:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item_date):
                    raise ValueError
                date.fromisoformat(item_date)
            except ValueError:
                return invalid("Choose a valid date.")
        price_cents = None
        if fields["price"]:
            if not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,2})?", fields["price"]):
                return invalid("Enter a non-negative price with up to two decimal places (maximum 999999999.99).")
            price_cents = int(Decimal(fields["price"]) * 100)
        if len(fields["description"]) > 5000:
            return invalid("Keep the description to 5,000 characters or fewer.")
        upload = form.get("image")
        if not name or len(name) > 120:
            return invalid("Enter a name between 1 and 120 characters.")
        picture = None
        has_upload = isinstance(upload, UploadFile) and bool(upload.filename)
        if not has_upload and item is None:
            return invalid("Choose an image to add your item.")
        if has_upload:
            content = await upload.read(MAX_IMAGE_BYTES + 1)
            if len(content) > MAX_IMAGE_BYTES:
                return invalid("Choose an image under 10 MB.")
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
                return invalid("Choose a valid JPEG, PNG, or WebP image (up to 20 million pixels).")
    item_id = item["id"] if item is not None else uuid.uuid4().hex
    filename = f"{uuid.uuid4().hex}.jpg" if picture is not None else None
    path = DATA_DIR / "uploads" / filename if filename else None
    old_filename = None
    try:
        if picture is not None:
            picture.save(path, "JPEG", quality=85)
        with connect() as db:
            if item is None:
                db.execute(
                    "INSERT INTO items (id, name, image_path, created_at, item_date, price_cents, description) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item_id, name, filename, datetime.now(timezone.utc).isoformat(), item_date, price_cents, fields["description"] or None),
                )
            else:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT image_path FROM items WHERE id = ?", (item_id,)).fetchone()
                if current is None:
                    raise HTTPException(status_code=404, detail="Item not found")
                old_filename = current["image_path"] if filename else None
                db.execute(
                    "UPDATE items SET name = ?, item_date = ?, price_cents = ?, description = ?, image_path = ? WHERE id = ?",
                    (name, item_date, price_cents, fields["description"] or None, filename or current["image_path"], item_id),
                )
    except Exception:
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    if old_filename:
        (DATA_DIR / "uploads" / old_filename).unlink(missing_ok=True)
    return RedirectResponse("/", status_code=303)
