"""Tests for schepper CLI.

sbx calls are mocked by patching `schepper.sh`. Each mock returns fake
subprocess results based on command-string fragments — the same approach
sbx itself uses internally (--json flags drive structured output).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from schepper import NetworkConfig, SandboxConfig, cli, expand_ports, memory_to_mib, network_diff

# ---------------------------------------------------------------------------
# Shared test data  — realistic sbx --json payloads
# ---------------------------------------------------------------------------

SANDBOX_STOPPED = {
    "name": "myproject",
    "id": "abc123",
    "agent": "claude",
    "status": "stopped",
    "memory": "8g",
    "memory_mib": 8192,
    "workspaces": ["/home/user/Projects/myproject"],
    "created_at": "2026-01-01T00:00:00Z",
}

SANDBOX_RUNNING = {
    **SANDBOX_STOPPED,
    "status": "running",
    "detached": True,
}

PORT_3000 = {"host_port": 3000, "sandbox_port": 3000, "protocol": "tcp4"}
PORT_8000 = {"host_port": 8000, "sandbox_port": 8000, "protocol": "tcp4"}

CONFIG = """\
name: myproject
kit: claude
memory: 8g
cpus: 0
storage: 10g
ports: []
ssh: false
"""


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def make_sh(responses: dict):
    """Return a mock sh() that maps command fragments to fake CompletedProcess results.

    Keys are substrings matched against ' '.join(cmd). First match wins, so
    put more specific keys before less specific ones.
    """
    def mock_sh(cmd, check=True, capture=False, extra_env=None):
        cmd_str = " ".join(str(c) for c in cmd)
        for fragment, resp in responses.items():
            if fragment in cmd_str:
                r = MagicMock()
                r.stdout = resp.get("stdout", "")
                r.returncode = resp.get("returncode", 0)
                return r
        r = MagicMock()
        r.stdout = ""
        r.returncode = 0
        return r
    return mock_sh


def policy_rule(decision, resources, created_via="added", scope="sandbox:myproject"):
    return {
        "id": f"{decision}-{resources[0]}",
        "scope": scope,
        "applies_to": scope,
        "resource_type": "network",
        "decision": decision,
        "resources": resources,
        "provenance": {"created_via": created_via},
    }


def default_sh(sandboxes=None, ports=None, rules=None):
    """Standard sbx mock used by most up/status/run tests."""
    return make_sh({
        "sbx ls --json":              {"stdout": json.dumps(sandboxes or [])},
        "sbx ports myproject --json": {"stdout": json.dumps(ports or [])},
        "sbx policy ls myproject --json": {"stdout": json.dumps({"rules": rules or []})},
    })


def tracking(base):
    calls = []

    def tracking_sh(cmd, **kwargs):
        calls.append(list(cmd))
        return base(cmd, **kwargs)
    return calls, tracking_sh


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# Unit: expand_ports
# ---------------------------------------------------------------------------

class TestExpandPorts:
    def test_single_int(self):
        assert expand_ports([3000]) == [3000]

    def test_single_str(self):
        assert expand_ports(["3000"]) == [3000]

    def test_range(self):
        assert expand_ports(["8000-8002"]) == [8000, 8001, 8002]

    def test_mixed(self):
        assert expand_ports(["3000", "8000-8001"]) == [3000, 8000, 8001]

    def test_sorted(self):
        assert expand_ports(["8001", "3000"]) == [3000, 8001]


# ---------------------------------------------------------------------------
# Unit: memory_to_mib
# ---------------------------------------------------------------------------

class TestMemoryToMib:
    def test_gigabytes(self):
        assert memory_to_mib("8g") == 8192

    def test_megabytes(self):
        assert memory_to_mib("512m") == 512

    def test_large(self):
        assert memory_to_mib("16g") == 16384

    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError, match="unexpected size unit"):
            memory_to_mib("8k")


# ---------------------------------------------------------------------------
# Pydantic model validation
# ---------------------------------------------------------------------------

class TestSandboxConfig:
    def test_valid_config(self):
        c = SandboxConfig(name="test", memory="8g", storage="10g")
        assert c.name == "test"
        assert c.memory == "8g"

    def test_defaults_applied(self):
        c = SandboxConfig()
        assert c.kit == "claude"
        assert c.memory == "8g"
        assert c.cpus == 0
        assert c.storage == "10g"
        assert c.ports == []
        assert c.ssh is False

    def test_normalises_8gb_to_8g(self):
        c = SandboxConfig(memory="8gb")
        assert c.memory == "8g"

    def test_invalid_memory_unit(self):
        with pytest.raises(ValidationError) as exc:
            SandboxConfig(memory="999tb")
        assert "memory" in str(exc.value)

    def test_double_b_suffix_rejected(self):
        with pytest.raises(ValidationError):
            SandboxConfig(memory="8gbb")

    def test_port_out_of_range(self):
        with pytest.raises(ValidationError):
            SandboxConfig(ports=[99999])

    def test_negative_port_rejected(self):
        with pytest.raises(ValidationError):
            SandboxConfig(ports=[-3000])

    def test_port_range_inverted(self):
        with pytest.raises(ValidationError):
            SandboxConfig(ports=["100-50"])

    def test_port_range_out_of_bounds(self):
        with pytest.raises(ValidationError):
            SandboxConfig(ports=["80000-80010"])

    def test_valid_port_range(self):
        c = SandboxConfig(ports=["8000-8002"])
        assert c.ports == ["8000-8002"]

    def test_negative_cpus_rejected(self):
        with pytest.raises(ValidationError):
            SandboxConfig(cpus=-1)

    def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError):
            SandboxConfig(unknown_key="bad")


# ---------------------------------------------------------------------------
# CLI: init
# ---------------------------------------------------------------------------

class TestInit:
    def test_creates_schepper_yaml(self, runner):
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["init"])
        assert result.exit_code == 0
        assert "Created" in result.output

    def test_does_not_overwrite_existing(self, runner):
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            result = runner.invoke(cli, ["init"])
        assert result.exit_code == 0
        assert "already exists" in result.output

    def test_name_defaults_to_cwd_name(self, runner):
        with runner.isolated_filesystem():
            runner.invoke(cli, ["init"])
            content = Path("schepper.yaml").read_text()
        # name must be non-empty (set from cwd)
        lines = {l.split(":")[0].strip(): l for l in content.splitlines() if ":" in l}
        assert lines.get("name", "name: ").split(":", 1)[1].strip() != ""


# ---------------------------------------------------------------------------
# CLI: up
# ---------------------------------------------------------------------------

class TestUp:
    def test_creates_new_sandbox_after_confirm(self, runner):
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", default_sh()):
                result = runner.invoke(cli, ["up"], input="y\n")
        assert result.exit_code == 0
        assert "Create?" in result.output
        assert "created" in result.output

    def test_aborts_create_on_no(self, runner):
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", default_sh()):
                result = runner.invoke(cli, ["up"], input="n\n")
        assert result.exit_code != 0

    def test_existing_sandbox_no_changes(self, runner):
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", default_sh(sandboxes=[SANDBOX_RUNNING])):
                result = runner.invoke(cli, ["up"])
        assert result.exit_code == 0
        assert "up to date" in result.output

    def test_port_add_shows_diff_and_confirms(self, runner):
        config = CONFIG.replace("ports: []", "ports:\n  - 3000")
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(config)
            with patch("schepper.sh", default_sh(sandboxes=[SANDBOX_RUNNING], ports=[])):
                result = runner.invoke(cli, ["up"], input="y\n")
        assert result.exit_code == 0
        assert "+ 3000:3000" in result.output

    def test_port_remove_shows_diff_and_confirms(self, runner):
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)  # no ports desired
            with patch("schepper.sh", default_sh(sandboxes=[SANDBOX_RUNNING], ports=[PORT_3000])):
                result = runner.invoke(cli, ["up"], input="y\n")
        assert result.exit_code == 0
        assert "- 3000:3000" in result.output

    def test_port_reconcile_abort(self, runner):
        config = CONFIG.replace("ports: []", "ports:\n  - 3000")
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(config)
            with patch("schepper.sh", default_sh(sandboxes=[SANDBOX_RUNNING], ports=[])):
                result = runner.invoke(cli, ["up"], input="n\n")
        assert result.exit_code != 0

    def test_memory_change_warns_recreation(self, runner):
        config = CONFIG.replace("memory: 8g", "memory: 16g")
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(config)
            with patch("schepper.sh", default_sh(sandboxes=[SANDBOX_STOPPED])):
                result = runner.invoke(cli, ["up"], input="n\n")
        assert "memory" in result.output
        assert "recreat" in result.output.lower()

    def test_kit_change_warns_recreation(self, runner):
        config = CONFIG.replace("kit: claude", "kit: codex")
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(config)
            with patch("schepper.sh", default_sh(sandboxes=[SANDBOX_STOPPED])):
                result = runner.invoke(cli, ["up"], input="n\n")
        assert "kit" in result.output
        assert "recreat" in result.output.lower()

    def test_storage_passed_as_env_var_on_create(self, runner):
        calls = []
        base = default_sh()

        def tracking_sh(cmd, check=True, capture=False, extra_env=None):
            calls.append({"cmd": list(cmd), "extra_env": extra_env})
            return base(cmd, check=check, capture=capture)

        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", tracking_sh):
                runner.invoke(cli, ["up"], input="y\n")

        create_call = next((c for c in calls if "create" in c["cmd"]), None)
        assert create_call is not None
        assert create_call["extra_env"] == {"DOCKER_SANDBOXES_ROOT_SIZE": "10g"}

    def test_starts_stopped_sandbox(self, runner):
        calls = []
        base = default_sh(sandboxes=[SANDBOX_STOPPED])

        def tracking_sh(cmd, **kwargs):
            calls.append(list(cmd))
            return base(cmd, **kwargs)

        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", tracking_sh):
                runner.invoke(cli, ["up"])

        assert any(
            "run" in c and "--detach" in c and "myproject" in c
            for c in calls
        )

    def test_does_not_start_already_running_sandbox(self, runner):
        calls = []
        base = default_sh(sandboxes=[SANDBOX_RUNNING])

        def tracking_sh(cmd, **kwargs):
            calls.append(list(cmd))
            return base(cmd, **kwargs)

        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", tracking_sh):
                runner.invoke(cli, ["up"])

        assert not any(
            "run" in c and "--detach" in c
            for c in calls
        )

    def test_no_config_exits(self, runner):
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["up"])
        assert result.exit_code != 0

    def test_invalid_config_exits_with_errors(self, runner):
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text("memory: 999tb\n")
            result = runner.invoke(cli, ["up"])
        assert result.exit_code != 0
        assert "memory" in result.output


# ---------------------------------------------------------------------------
# CLI: run
# ---------------------------------------------------------------------------

class TestRun:
    def test_attaches_when_sandbox_exists(self, runner):
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", default_sh(sandboxes=[SANDBOX_RUNNING])):
                result = runner.invoke(cli, ["run"])
        assert result.exit_code == 0

    def test_fails_when_sandbox_missing(self, runner):
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", default_sh(sandboxes=[])):
                result = runner.invoke(cli, ["run"])
        assert result.exit_code != 0
        assert "does not exist" in result.output


# ---------------------------------------------------------------------------
# CLI: status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_not_created(self, runner):
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", default_sh(sandboxes=[])):
                result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "not created" in result.output

    def test_running_with_port(self, runner):
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            sh = default_sh(sandboxes=[SANDBOX_RUNNING], ports=[PORT_3000])
            with patch("schepper.sh", sh):
                result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "running" in result.output
        assert "3000" in result.output

    def test_stopped(self, runner):
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", default_sh(sandboxes=[SANDBOX_STOPPED])):
                result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "stopped" in result.output


# ---------------------------------------------------------------------------
# CLI: inspect
# ---------------------------------------------------------------------------

class TestInspect:
    def test_by_name_produces_valid_yaml(self, runner):
        sh = make_sh({
            "sbx ls --json":              {"stdout": json.dumps([SANDBOX_STOPPED])},
            "sbx ports myproject --json": {"stdout": json.dumps([PORT_3000])},
            "sandbox.disk.dockerVolume":  {"stdout": "10g"},
        })
        with runner.isolated_filesystem():
            with patch("schepper.sh", sh):
                result = runner.invoke(cli, ["inspect", "myproject"])
        assert result.exit_code == 0
        assert "name: myproject" in result.output
        assert "kit: claude" in result.output
        assert "3000" in result.output

    def test_missing_sandbox_exits(self, runner):
        sh = make_sh({"sbx ls --json": {"stdout": json.dumps([])}})
        with runner.isolated_filesystem():
            with patch("schepper.sh", sh):
                result = runner.invoke(cli, ["inspect", "nope"])
        assert result.exit_code != 0

    def test_storage_defaults_to_10g_in_output(self, runner):
        sh = make_sh({
            "sbx ls --json":              {"stdout": json.dumps([SANDBOX_STOPPED])},
            "sbx ports myproject --json": {"stdout": json.dumps([])},
        })
        with runner.isolated_filesystem():
            with patch("schepper.sh", sh):
                result = runner.invoke(cli, ["inspect", "myproject"])
        assert result.exit_code == 0
        assert "storage: 10g" in result.output


# ---------------------------------------------------------------------------
# CLI: down / rm
# ---------------------------------------------------------------------------

class TestDown:
    def test_calls_sbx_stop(self, runner):
        calls = []

        def tracking_sh(cmd, **kwargs):
            calls.append(list(cmd))
            r = MagicMock(); r.stdout = ""; r.returncode = 0
            return r

        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", tracking_sh):
                result = runner.invoke(cli, ["down"])

        assert result.exit_code == 0
        assert any("stop" in c and "myproject" in c for c in calls)

    def test_already_stopped_exits_with_message(self, runner):
        def failing_sh(cmd, **kwargs):
            r = MagicMock(); r.stdout = ""; r.returncode = 1
            return r

        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", failing_sh):
                result = runner.invoke(cli, ["down"])

        assert result.exit_code != 0
        assert "already stopped" in result.output


class TestRm:
    def test_removes_after_confirm(self, runner):
        calls = []

        def tracking_sh(cmd, **kwargs):
            calls.append(list(cmd))
            r = MagicMock(); r.stdout = ""; r.returncode = 0
            return r

        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", tracking_sh):
                result = runner.invoke(cli, ["rm"], input="y\n")

        assert result.exit_code == 0
        assert any("rm" in c and "myproject" in c for c in calls)

    def test_aborts_on_no(self, runner):
        calls = []

        def tracking_sh(cmd, **kwargs):
            calls.append(list(cmd))
            r = MagicMock(); r.stdout = ""; r.returncode = 0
            return r

        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", tracking_sh):
                result = runner.invoke(cli, ["rm"], input="n\n")

        assert result.exit_code != 0
        assert not any("rm" in c for c in calls)

    def test_sbx_rm_failure_exits_with_message(self, runner):
        def failing_sh(cmd, **kwargs):
            r = MagicMock(); r.stdout = ""; r.returncode = 1
            return r

        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", failing_sh):
                result = runner.invoke(cli, ["rm"], input="y\n")

        assert result.exit_code != 0
        assert "Could not remove" in result.output


# ---------------------------------------------------------------------------
# Network allow / deny lists
# ---------------------------------------------------------------------------

NET_CONFIG = CONFIG + """\
network:
  allow:
    - api.example.com
    - "*.pypi.org:443"
  deny:
    - evil.example.com
"""


class TestNetworkConfig:
    def test_defaults_empty(self):
        c = SandboxConfig(name="x")
        assert c.network.allow == [] and c.network.deny == []

    def test_dedupes_and_strips(self):
        assert NetworkConfig(allow=[" a.com ", "a.com"]).allow == ["a.com"]

    def test_overlap_rejected(self):
        with pytest.raises(ValidationError):
            NetworkConfig(allow=["a.com"], deny=["a.com"])

    def test_comma_rejected(self):
        with pytest.raises(ValidationError):
            NetworkConfig(allow=["a.com,b.com"])

    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError):
            SandboxConfig(name="x", network={"permit": ["a.com"]})

    def test_diff_removes_before_adding(self):
        desired = NetworkConfig(deny=["a.com"])
        current = {"allow": {"a.com"}, "deny": set()}
        assert network_diff(desired, current) == [("-", "allow", "a.com"), ("+", "deny", "a.com")]


class TestNetworkUp:
    def test_create_passes_deny_and_adds_allow(self, runner):
        calls, sh = tracking(default_sh())
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(NET_CONFIG)
            with patch("schepper.sh", sh):
                result = runner.invoke(cli, ["up"], input="y\n")
        assert result.exit_code == 0, result.output
        create = next(c for c in calls if "create" in c)
        assert create[create.index("--deny-network") + 1] == "evil.example.com"
        assert ["sbx", "policy", "allow", "network", "--sandbox", "myproject", "api.example.com"] in calls
        assert ["sbx", "policy", "allow", "network", "--sandbox", "myproject", "*.pypi.org:443"] in calls

    def test_existing_in_sync_is_up_to_date(self, runner):
        rules = [
            policy_rule("allow", ["api.example.com", "*.pypi.org:443"]),
            policy_rule("deny", ["evil.example.com"]),
        ]
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(NET_CONFIG)
            with patch("schepper.sh", default_sh(sandboxes=[SANDBOX_RUNNING], rules=rules)):
                result = runner.invoke(cli, ["up"])
        assert result.exit_code == 0, result.output
        assert "up to date" in result.output

    def test_reconciles_added_and_removed(self, runner):
        rules = [policy_rule("allow", ["api.example.com", "stale.example.com"])]
        calls, sh = tracking(default_sh(sandboxes=[SANDBOX_RUNNING], rules=rules))
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(NET_CONFIG)
            with patch("schepper.sh", sh):
                result = runner.invoke(cli, ["up"], input="y\n")
        assert result.exit_code == 0, result.output
        assert "- allow stale.example.com" in result.output
        assert "+ allow *.pypi.org:443" in result.output
        assert "+ deny evil.example.com" in result.output
        assert ["sbx", "policy", "rm", "network", "--sandbox", "myproject",
                "--resource", "stale.example.com", "--force"] in calls
        assert ["sbx", "policy", "deny", "network", "--sandbox", "myproject", "evil.example.com"] in calls

    def test_ignores_provisioned_and_global_rules(self, runner):
        rules = [
            policy_rule("allow", ["api.anthropic.com:443"], created_via="provisioned"),
            policy_rule("allow", ["github.com:443"], created_via="default", scope="global"),
        ]
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", default_sh(sandboxes=[SANDBOX_RUNNING], rules=rules)):
                result = runner.invoke(cli, ["up"])
        assert result.exit_code == 0, result.output
        assert "up to date" in result.output

    def test_network_reconcile_abort(self, runner):
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(NET_CONFIG)
            with patch("schepper.sh", default_sh(sandboxes=[SANDBOX_RUNNING])):
                result = runner.invoke(cli, ["up"], input="n\n")
        assert result.exit_code != 0

    def test_status_and_inspect_show_rules(self, runner):
        rules = [policy_rule("allow", ["api.example.com"]), policy_rule("deny", ["evil.example.com"])]
        with runner.isolated_filesystem():
            Path("schepper.yaml").write_text(CONFIG)
            with patch("schepper.sh", default_sh(sandboxes=[SANDBOX_RUNNING], rules=rules)):
                status = runner.invoke(cli, ["status"])
                inspect = runner.invoke(cli, ["inspect"])
        assert "allow:     api.example.com" in status.output
        assert "deny:      evil.example.com" in status.output
        assert "- api.example.com" in inspect.output
        assert "- evil.example.com" in inspect.output
