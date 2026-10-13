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
