"""An MCP extension must not inherit the parent process's secrets (GAP B11).

`MCPConnection.start` used to launch third-party servers with
`env={**os.environ, **extension.env}`. Every `npx`-installed MCP server on the
machine therefore received every API key, cloud credential and database URL the
shell was carrying. These tests pin the allowlist that replaced it, and the last
one proves the property end to end: a real child process is launched through the
real `MCPConnection.start`, and it reports back what it actually received.
"""

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

from djcode.extensions import (
    MCP_ENV_ALLOWED_PREFIXES,
    MCP_ENV_ALLOWLIST,
    Extension,
    MCPConnection,
    build_mcp_env,
)

# Names that must never reach a third-party subprocess. Every one of these is a
# real variable DJcode itself or its ecosystem sets or reads.
SECRETS = {
    "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI-test-not-a-real-key",
    "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "OPENAI_API_KEY": "sk-openai-test",
    "GITHUB_TOKEN": "ghp_test",
    "DATABASE_URL": "postgres://user:hunter2@localhost/db",
    "DJCODE_ACCOUNT_TOKEN": "djc-test",
    "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
}


def _environ(**extra):
    """A parent environment carrying both allowed names and secrets."""
    env = {
        "PATH": "/usr/bin",
        "HOME": "/home/tester",
        "USERPROFILE": r"C:\Users\tester",
        "USER": "tester",
        "USERNAME": "tester",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TERM": "xterm-256color",
        "SHELL": "/bin/bash",
        "COMSPEC": r"C:\Windows\system32\cmd.exe",
        "SYSTEMROOT": r"C:\Windows",
        "TMPDIR": "/tmp",
        "TEMP": r"C:\Temp",
        "TMP": r"C:\Temp",
        "XDG_CONFIG_HOME": "/home/tester/.config",
        "XDG_DATA_HOME": "/home/tester/.local/share",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }
    env.update(SECRETS)
    env.update(extra)
    return env


def test_secrets_never_reach_the_child_env():
    env = build_mcp_env({}, _environ())
    for name in SECRETS:
        assert name not in env, f"{name} leaked into the MCP subprocess environment"


def test_every_allowlisted_name_is_passed_through():
    source = _environ()
    env = build_mcp_env({}, source)
    for name in MCP_ENV_ALLOWLIST:
        assert env[name] == source[name]


def test_xdg_variables_are_passed_through_by_prefix():
    env = build_mcp_env({}, _environ(XDG_SESSION_TYPE="wayland"))
    assert env["XDG_CONFIG_HOME"] == "/home/tester/.config"
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert env["XDG_SESSION_TYPE"] == "wayland"


def test_nothing_outside_the_allowlist_survives():
    source = _environ(TOTALLY_NEW_CREDENTIAL="s3cret", XDG_CACHE_HOME="/c")
    env = build_mcp_env({}, source)
    unexpected = {
        name
        for name in env
        if name not in MCP_ENV_ALLOWLIST and not name.startswith(MCP_ENV_ALLOWED_PREFIXES)
    }
    assert unexpected == set()
    assert "TOTALLY_NEW_CREDENTIAL" not in env


def test_declared_env_is_the_only_way_a_secret_gets_in():
    """The extension's own config is the consent channel."""
    env = build_mcp_env({"GITHUB_TOKEN": "ghp_declared"}, _environ())
    assert env["GITHUB_TOKEN"] == "ghp_declared"
    # Declaring one secret does not open the door for the rest.
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_declared_empty_value_passes_the_parent_value_through():
    """So a user need not copy a live secret into extensions.json."""
    env = build_mcp_env({"AWS_SECRET_ACCESS_KEY": ""}, _environ())
    assert env["AWS_SECRET_ACCESS_KEY"] == SECRETS["AWS_SECRET_ACCESS_KEY"]
    assert "AWS_ACCESS_KEY_ID" not in env


def test_declared_pass_through_of_an_unset_variable_is_empty_not_missing():
    env = build_mcp_env({"NOT_SET_ANYWHERE": ""}, _environ())
    assert env["NOT_SET_ANYWHERE"] == ""


def test_declared_env_overrides_an_allowlisted_name():
    env = build_mcp_env({"PATH": "/opt/sandbox/bin"}, _environ())
    assert env["PATH"] == "/opt/sandbox/bin"


def test_defaults_to_the_real_process_environment(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leaked-if-you-see-me")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/tester/.config")
    env = build_mcp_env()
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["XDG_CONFIG_HOME"] == "/home/tester/.config"
    assert "PATH" in env  # the child still has to be able to find its own tools


# ---------------------------------------------------------------------------
# End-to-end: a real subprocess, launched by the real MCPConnection.start
# ---------------------------------------------------------------------------

# A minimal MCP server: it answers `initialize`, and as its first act it writes
# the environment it was actually given to a file the test reads back.
_STUB_SERVER = textwrap.dedent(
    """
    import json, os, sys

    dump = sys.argv[1]
    with open(dump, "w", encoding="utf-8") as handle:
        json.dump(dict(os.environ), handle)

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if message.get("method") == "initialize":
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "serverInfo": {"name": "stub", "version": "0"},
                        },
                    }
                )
                + "\\n"
            )
            sys.stdout.flush()
    """
)


def test_real_subprocess_never_sees_aws_secret_access_key(tmp_path, monkeypatch):
    script = tmp_path / "stub_mcp_server.py"
    script.write_text(_STUB_SERVER, encoding="utf-8")
    dump = tmp_path / "child-env.json"

    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    extension = Extension(
        name="stub",
        cmd=sys.executable,
        args=[str(script), str(dump)],
        env={"STUB_DECLARED": "declared-value"},
    )
    connection = MCPConnection(extension)

    # Positive control: without this the whole test could pass vacuously.
    assert os.environ["AWS_SECRET_ACCESS_KEY"] == SECRETS["AWS_SECRET_ACCESS_KEY"]

    async def drive():
        await connection.start()
        await connection.stop()

    asyncio.run(drive())

    child_env = json.loads(Path(dump).read_text(encoding="utf-8"))

    # The headline assertion: the secret the audit named is not there.
    assert "AWS_SECRET_ACCESS_KEY" not in child_env

    for name in SECRETS:
        assert name not in child_env, f"{name} reached the MCP server process"

    # ...and the child is still usable: it can find interpreters and its config.
    assert child_env.get("PATH") == os.environ.get("PATH")
    assert child_env["STUB_DECLARED"] == "declared-value"
    assert child_env["XDG_CONFIG_HOME"] == str(tmp_path / "config")
