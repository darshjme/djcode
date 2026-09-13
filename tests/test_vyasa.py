import httpx
import pytest
from click.testing import CliRunner

from djcode.cli import main
from djcode.vyasa import endpoint, request_fleet


def test_client_request_and_prefix(monkeypatch):
    monkeypatch.setenv("VYASA_URL", "https://fleet.example/api/vyasa")
    monkeypatch.setenv("VYASA_TOKEN", "vya_live_fixture")

    def handler(request):
        assert str(request.url) == "https://fleet.example/api/vyasa/v1/chat"
        assert request.headers["Authorization"] == "Bearer vya_live_fixture"
        return httpx.Response(200, json={"text": "reply"})

    assert (
        request_fleet("hello", employee="prometheus", transport=httpx.MockTransport(handler))[
            "text"
        ]
        == "reply"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://fleet.example",
        "https://user:pass@fleet.example",
        "https://fleet.example?secret=x",
        "file:///tmp/server",
    ],
)
def test_reject_unsafe_endpoint(monkeypatch, url):
    monkeypatch.setenv("VYASA_URL", url)
    with pytest.raises(ValueError):
        endpoint()


def test_no_token_and_no_redirect(monkeypatch):
    monkeypatch.delenv("VYASA_TOKEN", raising=False)
    with pytest.raises(ValueError, match="VYASA_TOKEN"):
        request_fleet()
    monkeypatch.setenv("VYASA_TOKEN", "vya_live_fixture")
    monkeypatch.setenv("VYASA_URL", "https://fleet.example")
    with pytest.raises(ValueError, match="302"):
        request_fleet(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(302, headers={"Location": "https://elsewhere.example"})
            )
        )


def test_cli_bypasses_model_setup_and_local_updates(monkeypatch):
    import djcode.vyasa

    monkeypatch.setattr(
        djcode.vyasa,
        "request_fleet",
        lambda *a, **kw: {
            "employees": [{"id": "prometheus", "name": "Prometheus", "enabled": True}]
        },
    )
    result = CliRunner().invoke(main, ["--vyasa"])
    assert result.exit_code == 0, result.output
    assert "prometheus" in result.output
    assert CliRunner().invoke(main, ["--vyasa-employee", "prometheus"]).exit_code == 2


def test_update_manifest_uses_source_tag_without_actions():
    from djcode import managed_update as updater

    commit = "a" * 40
    manifest = {
        "schema": 2,
        "repository": updater.REPOSITORY,
        "branch": "main",
        "commit": commit,
        "version": "4.3.0",
        "sha256": "b" * 64,
        "wheel_url": f"https://github.com/{updater.REPOSITORY}/releases/download/build-{commit[:12]}/djcode-4.3.0-py3-none-any.whl",
    }

    def handler(request):
        assert "/actions/" not in str(request.url)
        if "update.json" in str(request.url):
            return httpx.Response(200, json=manifest)
        return httpx.Response(200, json={"object": {"type": "commit", "sha": commit}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert updater.verified_manifest(client) == manifest
