"""An unhandled error answers a JSON 500 that the browser can read (CORS headers)."""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.common.server_errors import JsonServerErrors


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/boom")
    def boom():
        raise RuntimeError("integrity error")

    # Same order as main.py: added first means inside CORS.
    app.add_middleware(JsonServerErrors)
    app.add_middleware(CORSMiddleware, allow_origins=["http://site.test"], allow_methods=["*"])
    return app


def test_an_unhandled_error_is_a_json_500_with_cors_headers():
    response = TestClient(_app(), raise_server_exceptions=False).get(
        "/boom", headers={"Origin": "http://site.test"}
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert response.headers["access-control-allow-origin"] == "http://site.test"
