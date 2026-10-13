from io import BytesIO
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    mount = next(route for route in main.app.routes if getattr(route, "path", None) == "/uploads")
    monkeypatch.setattr(mount.app, "directory", str(tmp_path / "uploads"))
    monkeypatch.setattr(mount.app, "all_directories", [str(tmp_path / "uploads")])
    with TestClient(main.app) as browser:
        yield browser


def photo():
    output = BytesIO()
    Image.new("RGB", (1400, 800), "green").save(output, "PNG")
    return output.getvalue()


def test_create_list_image_and_restart(client):
    assert "Make room for your first item" in client.get("/").text
    response = client.post("/items", data={"name": "  Camera <script>  "}, files={"image": ("../../camera.png", photo(), "image/png")})
    assert response.status_code == 200
    assert "Camera &lt;script&gt;" in response.text
    with main.connect() as db:
        item = db.execute("SELECT * FROM items").fetchone()
    assert item["name"] == "Camera <script>"
    image = client.get(f'/uploads/{item["image_path"]}')
    assert image.status_code == 200
    with Image.open(BytesIO(image.content)) as saved:
        assert saved.format == "JPEG"
        assert max(saved.size) == 1200
        assert not saved.getexif()
    with TestClient(main.app) as restarted:
        assert "Camera &lt;script&gt;" in restarted.get("/").text
        assert restarted.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("name,content", [(" ", photo()), ("x" * 121, photo()), ("Item", b"not an image")])
def test_invalid_upload_leaves_no_data(client, name, content):
    response = client.post("/items", data={"name": name}, files={"image": ("image.png", content, "image/png")})
    assert response.status_code == 400
    with main.connect() as db:
        assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    assert list((main.DATA_DIR / "uploads").iterdir()) == []


def test_missing_and_large_upload(client, monkeypatch):
    assert client.post("/items", data={"name": "Item"}).status_code == 400
    monkeypatch.setattr(main, "MAX_REQUEST_BYTES", 200)
    assert client.post("/items", content=b"x" * 201).status_code == 413
    assert client.post("/items", content=iter([b"x" * 100, b"x" * 101])).status_code == 413


def test_database_failure_removes_photo(client, monkeypatch):
    def broken_connection():
        raise sqlite3.OperationalError("unavailable")
    monkeypatch.setattr(main, "connect", broken_connection)
    with pytest.raises(sqlite3.OperationalError):
        client.post("/items", data={"name": "Item"}, files={"image": ("photo.png", photo(), "image/png")})
    assert list((main.DATA_DIR / "uploads").iterdir()) == []


def test_item_details_persist_and_display(client):
    details = {"name": "Camera", "item_date": "2026-10-13", "price": "123.45", "description": "First line\n<script>Details</script>"}
    response = client.post("/items", data=details, files={"image": ("photo.png", photo(), "image/png")})
    assert response.status_code == 200
    assert "2026-10-13" in response.text
    assert "123.45" in response.text
    assert "&lt;script&gt;Details&lt;/script&gt;" in response.text
    with main.connect() as db:
        item = db.execute("SELECT * FROM items").fetchone()
        assert item["price_cents"] == 12345
        assert item["item_date"] == details["item_date"]
        assert item["description"] == details["description"]
    with TestClient(main.app) as restarted:
        assert "123.45" in restarted.get("/").text
    response = client.post("/items", data={"name": "Free item", "price": "0"}, files={"image": ("photo.png", photo(), "image/png")})
    assert "0.00" in response.text


@pytest.mark.parametrize("field,value", [
    ("item_date", "2026-02-30"), ("item_date", "20261013"),
    ("price", "-1"), ("price", "12.345"), ("price", "NaN"),
    ("price", "1000000000"), ("description", "x" * 5001),
])
def test_invalid_details_do_not_create_items(client, field, value):
    data = {"name": "Camera", "item_date": "2026-10-13", "price": "123.45", "description": "Keep this text"}
    data[field] = value
    response = client.post("/items", data=data, files={"image": ("photo.png", photo(), "image/png")})
    assert response.status_code == 400
    assert 'value="Camera"' in response.text
    if field != "description":
        assert "Keep this text" in response.text
    with main.connect() as db:
        assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    assert list((main.DATA_DIR / "uploads").iterdir()) == []


def test_migration_preserves_existing_items_and_is_repeatable(client):
    image_path = main.DATA_DIR / "uploads" / "legacy.jpg"
    image_path.write_bytes(photo())
    with main.connect() as db:
        db.execute("DROP TABLE items")
        db.execute("CREATE TABLE items (id TEXT PRIMARY KEY, name TEXT NOT NULL, image_path TEXT NOT NULL, created_at TEXT NOT NULL)")
        db.execute("INSERT INTO items VALUES (?, ?, ?, ?)", ("legacy", "Old camera", "legacy.jpg", "2026-01-01T00:00:00+00:00"))
    for _ in range(2):
        with TestClient(main.app) as restarted:
            assert "Old camera" in restarted.get("/").text
        with main.connect() as db:
            item = db.execute("SELECT * FROM items WHERE id = 'legacy'").fetchone()
            assert item["image_path"] == "legacy.jpg"
            assert item["item_date"] is None
            assert item["price_cents"] is None
            assert item["description"] is None
        assert image_path.read_bytes() == photo()


def test_export_empty_collection(client):
    import csv
    from io import StringIO
    response = client.get('/items/export.csv')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/csv')
    assert response.headers['content-disposition'] == 'attachment; filename="itemkeeper-items.csv"'
    reader = csv.DictReader(StringIO(response.content.decode('utf-8-sig'), newline=''))
    assert reader.fieldnames == ['id', 'name', 'image_path', 'created_at', 'item_date', 'price_cents', 'description', 'price']
    assert list(reader) == []
    assert '/items/export.csv' in client.get('/').text


def test_export_all_database_fields_losslessly(client):
    import csv
    from io import StringIO
    records = [
        ('one', 'Camera, "旅行"', 'one.jpg', '2026-10-13T00:00:00+00:00', '2026-10-12', 12345, 'First line\r\nSecond, "quoted" line ☕'),
        ('two', '=literal text', 'two.jpg', '2026-10-12T00:00:00+00:00', None, None, None),
        ('three', 'Free', 'three.jpg', '2026-10-11T00:00:00+00:00', None, 0, 'Free item'),
    ]
    with main.connect() as db:
        db.executemany('INSERT INTO items (id, name, image_path, created_at, item_date, price_cents, description) VALUES (?, ?, ?, ?, ?, ?, ?)', records)
        expected = [dict(row) for row in db.execute('SELECT * FROM items ORDER BY created_at DESC, id DESC')]
    response = client.get('/items/export.csv')
    exported = list(csv.DictReader(StringIO(response.content.decode('utf-8-sig'), newline='')))
    assert len(exported) == 3
    for original, saved in zip(expected, exported):
        for field, value in original.items():
            assert saved[field] == (str(value) if value is not None else '')
    assert [row['price'] for row in exported] == ['123.45', '', '0.00']
    with main.connect() as db:
        assert db.execute('SELECT count(*) FROM items').fetchone()[0] == 3
