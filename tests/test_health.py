from fastapi.testclient import TestClient

from app.main import create_app


def test_fastapi_app_starts() -> None:
    client = TestClient(create_app())
    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "DevMeeting AI Backend"