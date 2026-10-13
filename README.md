# Itemkeeper

A small personal inventory app: name an item, upload a photo, and browse your collection. FastAPI serves HTML and images; SQLite runs in the same container. No external services or browser build tools are needed.

## Run with Docker

```sh
docker compose up -d --build
```

Open http://localhost:8111. Compose stores the database and photos in the `itemkeeper-data` volume mounted at `/data`. Data survives container recreation and image upgrades. **`docker compose down -v` deletes that volume and all items.** The container runs as a non-root user and exposes a database-backed `/health` endpoint.

This version is intended for personal use on a trusted local network. It has no authentication. Add access control before exposing it to the Internet. To restrict access to this computer, change the port mapping to `127.0.0.1:8111:8111`.

## Local development

Use Python 3.12:

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
DATA_DIR="$PWD/.local-data" .venv/bin/uvicorn app.main:app --reload --port 8111
```

Run tests with `.venv/bin/python -m pytest -q`. No credentials are required.

Names must contain 1–120 characters. Photos must be JPEG, PNG, or WebP, at most 10 MB and 20 million pixels. Photos are converted to JPEG, resized to fit 1200 × 1200, and stripped of metadata; original files are not retained. Every item stores its name, generated image filename, unique ID, and UTC creation time. SQLite uses WAL mode and parameterized queries. A single server process is sufficient for this personal workflow.

## Backup and restore

For a consistent backup, stop the app and copy the entire `/data` volume (including any SQLite WAL files and `uploads/`), then restart it. Restore into a stopped container's volume and ensure UID 10001 can read and write it. Keep database and image backups together.

Editing, deletion, search, and accounts are future features.

### Builds behind an HTTPS proxy

If your build environment requires a custom CA, supply its trusted certificate bundle as an optional BuildKit secret (it is not stored in the image):

```sh
docker build --secret id=custom_ca,src=/path/to/trusted-ca-bundle.pem -t codex-playground-itemkeeper .
docker compose up -d --no-build
```

Normal Docker builds do not require this option. TLS verification remains enabled.

## Item details

Items can optionally include a date (using the browser date picker), a price, and a description. Prices are non-negative with up to two decimal places and are stored as integer cents for exact values; no currency is assumed. Descriptions support up to 5,000 characters. All details appear on the item cards. The database automatically adds these fields on startup while preserving existing items and photos; existing items have blank values for these fields.
