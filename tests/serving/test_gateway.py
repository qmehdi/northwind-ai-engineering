"""The gateway key from the secret store, the way nw.auth resolves the API key."""

import logging

from nw.config import Settings, Track
from nw.serving.gateway import gateway_fields, resolve_gateway_key


def _settings(**kw) -> Settings:
    return Settings(track=Track.AWS, gateway_url="https://gw.example", _env_file=None, **kw)


def test_aws_secret_arn_fills_the_key():
    arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:northwind-alice-gateway-key-AbCdEf"
    calls = []

    def fetch(value):
        calls.append(value)
        return "sk-alice \n"

    out = resolve_gateway_key(_settings(), {"NW_GATEWAY_KEY_SECRET_ARN": arn}, fetch_aws=fetch)
    assert out.gateway_key == "sk-alice" and calls == [arn]
    assert gateway_fields(out) == {"gateway": "https://gw.example", "gateway_key": "set"}


def test_gcp_secret_name_fills_the_key():
    name = "projects/p/secrets/northwind-alice-gateway-key/versions/latest"
    out = resolve_gateway_key(
        _settings(), {"NW_GATEWAY_KEY_SECRET_NAME": name}, fetch_gcp=lambda n: "sk-gcp"
    )
    assert out.gateway_key == "sk-gcp"


def test_key_in_environment_and_no_gateway_are_left_alone():
    def boom(_):
        raise AssertionError("must not fetch")

    with_key = _settings(gateway_key="sk-env")
    assert (
        resolve_gateway_key(with_key, {"NW_GATEWAY_KEY_SECRET_ARN": "arn:x"}, fetch_aws=boom)
        is with_key
    )
    direct = Settings(track=Track.LOCAL, _env_file=None)
    assert (
        resolve_gateway_key(direct, {"NW_GATEWAY_KEY_SECRET_ARN": "arn:x"}, fetch_aws=boom)
        is direct
    )
    assert gateway_fields(direct) == {"gateway": "direct", "gateway_key": "unset"}


def test_gateway_without_any_key_warns_and_keeps_settings(caplog):
    s = _settings()
    with caplog.at_level(logging.WARNING, logger="nw.serving.gateway"):
        assert resolve_gateway_key(s, {}) is s
    assert any(r.getMessage() == "gateway configured without a key" for r in caplog.records)
