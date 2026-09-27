import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nw.auth import install_api_key

pytestmark = pytest.mark.session01


def test_docs_are_not_open_on_a_keyed_service():
    app = FastAPI()
    install_api_key(app, key="k")
    c = TestClient(app)
    assert c.get("/docs").status_code == 401
    assert c.get("/openapi.json").status_code == 401
    assert c.get("/healthz").status_code in (200, 404)  # open, whatever the app serves there
