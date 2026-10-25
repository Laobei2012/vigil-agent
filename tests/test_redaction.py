import time
from pathlib import Path

import pytest

from vigil_agent.gateway.redaction import MASK, Redactor

FIXTURE = Path(__file__).parent / "fixtures" / "secrets.txt"


def _cases():
    for line in FIXTURE.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            # "<<>>" splits fake tokens so push protection does not flag the fixture.
            text, secret = (p.strip().replace("<<>>", "") for p in line.split("|||"))
            yield pytest.param(text, secret, id=text[:40])


@pytest.mark.parametrize(("text", "secret"), list(_cases()))
def test_every_fixture_secret_is_masked(text, secret):
    out = Redactor().text(text)
    assert secret not in out
    assert MASK in out


def test_nested_values_are_masked():
    data = {"a": [{"b": "token=abcdefabcdefabcdef"}], "c": {"d": ("AKIAIOSFODNN7EXAMPLE",)}}
    out = Redactor().value(data)
    assert "abcdefabcdefabcdef" not in str(out)
    assert "AKIAIOSFODNN7EXAMPLE" not in str(out)


def test_secret_looking_keys_are_masked_whole():
    out = Redactor().value({"API_KEY": "short", "replicas": "3"})
    assert out == {"API_KEY": MASK, "replicas": "3"}


def test_pod_env_values_and_flag_values_are_masked():
    container = {
        "env": [
            {"name": "DB_PASSWORD", "value": "hunter2real-inline"},
            {"name": "API_TOKEN", "value": "abc123def456ghi789"},
            {"name": "LOG_LEVEL", "value": "info"},
            {"name": "FROM_SECRET", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}},
        ],
        "args": ["--token", "tok-0123456789abcdef", "--port", "8080"],
    }
    out = Redactor().value(container)
    assert out["env"][:3] == [
        {"name": "DB_PASSWORD", "value": MASK},
        {"name": "API_TOKEN", "value": MASK},
        {"name": "LOG_LEVEL", "value": "info"},
    ]
    assert out["env"][3] == container["env"][3]
    assert out["args"] == ["--token", MASK, "--port", "8080"]


def test_pass_alone_is_a_secret_key_but_bypass_is_not():
    assert Redactor().value({"DB_PASS": "x", "bypass_cache": "true"}) == {"DB_PASS": MASK, "bypass_cache": "true"}


def test_author_keys_are_not_secrets():
    people = {"author": "alice", "commit_author": "bob", "authority": "ca.example"}
    assert Redactor().value(people) == people
    keys = ("auth", "AUTH_TOKEN", "authorization", "Proxy-Authorization", "authorisation", "x_authkey")
    assert Redactor().value(dict.fromkeys(keys, "s3cret")) == dict.fromkeys(keys, MASK)


@pytest.mark.parametrize(
    "text", ["a" * 200_000, "a1" * 100_000, "http://u:" * 22_000], ids=["letters", "letters_digits", "url_prefix"]
)
def test_redaction_time_grows_linearly(text):
    # The old url_password pattern needed about 9 minutes for 1 MB of letters.
    start = time.perf_counter()
    Redactor().text(text)
    assert time.perf_counter() - start < 1.0


def test_ordinary_text_is_unchanged():
    text = "GET /healthz 200 in 3ms for pod oci-cost-collector-a-29112 on node 10.0.1.36"
    assert Redactor().text(text) == text


def test_pii_is_off_by_default_and_available():
    text = "user alice@example.com from 10.1.2.3"
    assert Redactor().text(text) == text
    out = Redactor(pii=True).text(text)
    assert "alice@example.com" not in out and "10.1.2.3" not in out
