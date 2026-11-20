#!/usr/bin/env python3
"""schepper — declarative manager for Docker sbx sandboxes."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Union

import click
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

CONFIG_FILE = "schepper.yaml"

TEMPLATE = """\
name: {name}
kit: claude
memory: 8g
cpus: 0
storage: 10g
ports: []
ssh: false
network:
  allow: []
  deny: []
"""

_SIZE_RE = re.compile(r"^\d+[mg]$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------

class NetworkConfig(BaseModel):
    """Sandbox-scoped network policy rules (``sbx policy allow|deny network --sandbox``)."""

    model_config = ConfigDict(extra="forbid")

    allow: list[str] = []
    deny: list[str] = []

    @field_validator("allow", "deny")
    @classmethod
    def validate_resources(cls, v: list[str]) -> list[str]:
        result = []
        for raw in v:
            r = str(raw).strip()
            if not r:
                raise ValueError("empty network resource")
            if "," in r or any(c.isspace() for c in r):
                raise ValueError(f"'{raw}': list one resource per entry (no commas or spaces)")
            if r not in result:
                result.append(r)
        return result

    @model_validator(mode="after")
    def check_overlap(self) -> "NetworkConfig":
        both = sorted(set(self.allow) & set(self.deny))
        if both:
            raise ValueError(f"resources in both allow and deny: {', '.join(both)}")
        return self


class SandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    kit: str = "claude"
    memory: str = "8g"
    cpus: int = 0
    storage: str = "10g"
    ports: list[Union[str, int]] = []
    ssh: bool = False
    network: NetworkConfig = NetworkConfig()

    @field_validator("memory", "storage")
    @classmethod
    def validate_size(cls, v: str) -> str:
        normalized = v.lower().removesuffix("b")  # "8gb" → "8g"
        if not _SIZE_RE.match(normalized):
            raise ValueError(f"expected a size like '8g' or '512m', got '{v}'")
        return normalized

    @field_validator("cpus")
    @classmethod
    def validate_cpus(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be >= 0")
        return v

    @field_validator("ports")
    @classmethod
    def validate_ports(cls, v: list) -> list:
        for spec in v:
            s = str(spec).strip()
            if "-" in s and not s.startswith("-"):
                parts = s.split("-", 1)
                lo, hi = int(parts[0].strip()), int(parts[1].strip())
                if not (1 <= lo <= 65535 and 1 <= hi <= 65535):
                    raise ValueError(f"port range {s} contains out-of-range values")
                if lo > hi:
                    raise ValueError(f"port range {s}: start must be <= end")
            else:
                port = int(s)
                if not 1 <= port <= 65535:
                    raise ValueError(f"port {port} is out of range (1–65535)")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sh(cmd: list, check: bool = True, capture: bool = False, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **extra_env} if extra_env else None
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, env=env)


def load_config() -> SandboxConfig:
    path = Path(CONFIG_FILE)
    if not path.exists():
        click.echo(
            f"No {CONFIG_FILE} in current directory. Run 'schepper init' first.",
            err=True,
        )
        sys.exit(1)
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    if not raw.get("name"):
        raw["name"] = Path.cwd().name
    try:
        return SandboxConfig(**raw)
    except ValidationError as e:
        click.echo(f"Invalid {CONFIG_FILE}:", err=True)
        for err in e.errors():
            field = ".".join(str(x) for x in err["loc"])
            click.echo(f"  {field}: {err['msg']}", err=True)
        sys.exit(1)


def expand_ports(ports: list) -> list[int]:
    result = []
    for spec in ports:
        s = str(spec).strip()
        if "-" in s and not s.startswith("-"):
            lo, hi = s.split("-", 1)
            result.extend(range(int(lo.strip()), int(hi.strip()) + 1))
        else:
            result.append(int(s))
    return sorted(result)


def get_sandbox_info(name: str) -> dict | None:
    result = sh(["sbx", "ls", "--json"], check=False, capture=True)
    try:
        for s in json.loads(result.stdout):
            if s["name"] == name:
                return s
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _host_port(entry: dict) -> int | None:
    for key in ("host_port", "hostPort", "HostPort", "PublicPort"):
        v = entry.get(key)
        if v is not None:
            return int(v)
    return None


def get_published_ports(name: str) -> set[int]:
    result = sh(["sbx", "ports", name, "--json"], check=False, capture=True)
    if result.returncode != 0 or not result.stdout.strip():
        return set()
    try:
        ports = set()
        for entry in json.loads(result.stdout) or []:
            hp = _host_port(entry)
            if hp is not None:
                ports.add(hp)
        return ports
    except (json.JSONDecodeError, TypeError, ValueError):
        return set()


# Rules created by other means (e.g. "provisioned" by the kit, "default") are
# left alone; only rules a user added by hand are owned by schepper.yaml.
_MANAGED_RULE_ORIGINS = {"added"}


def get_network_rules(name: str) -> dict[str, set[str]]:
    """Return the sandbox-scoped, user-added network rules as {"allow": {...}, "deny": {...}}."""
    rules: dict[str, set[str]] = {"allow": set(), "deny": set()}
    result = sh(["sbx", "policy", "ls", name, "--json"], check=False, capture=True)
    if result.returncode != 0 or not result.stdout.strip():
        return rules
    try:
        data = json.loads(result.stdout) or {}
        for rule in data.get("rules") or []:
            if rule.get("scope") != f"sandbox:{name}":
                continue
            if rule.get("resource_type") != "network":
                continue
            if (rule.get("provenance") or {}).get("created_via") not in _MANAGED_RULE_ORIGINS:
                continue
            decision = rule.get("decision")
            if decision in rules:
                rules[decision].update(rule.get("resources") or [])
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return rules


def network_diff(desired: NetworkConfig, current: dict[str, set[str]]) -> list[tuple[str, str, str]]:
    """Return (op, decision, resource) tuples; removals first so a resource can switch sides."""
    changes = []
    for decision in ("allow", "deny"):
        want = set(getattr(desired, decision))
        for r in sorted(current[decision] - want):
            changes.append(("-", decision, r))
    for decision in ("allow", "deny"):
        want = set(getattr(desired, decision))
        for r in sorted(want - current[decision]):
            changes.append(("+", decision, r))
    return changes


def apply_network_changes(name: str, changes: list[tuple[str, str, str]]) -> None:
    for op, decision, resource in changes:
        if op == "-":
            cmd = ["sbx", "policy", "rm", "network", "--sandbox", name, "--resource", resource, "--force"]
        else:
            cmd = ["sbx", "policy", decision, "network", "--sandbox", name, resource]
        r = sh(cmd, check=False)
        if r.returncode != 0:
            verb = "remove" if op == "-" else "add"
            click.echo(f"  Warning: failed to {verb} {decision} rule for {resource}", err=True)


def memory_to_mib(s: str) -> int:
    s = s.lower().removesuffix("b")
    if s.endswith("g"):
        return int(s[:-1]) * 1024
    if s.endswith("m"):
        return int(s[:-1])
    raise ValueError(f"unexpected size unit in '{s}'")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """schepper — declarative manager for Docker sbx sandboxes."""


@cli.command()
def init():
    """Scaffold schepper.yaml in the current directory."""
    path = Path(CONFIG_FILE)
    if path.exists():
        click.echo(f"{CONFIG_FILE} already exists.")
        return
    path.write_text(TEMPLATE.format(name=Path.cwd().name))
    click.echo(f"Created {CONFIG_FILE} — edit it, then run 'schepper up'.")


@cli.command()
def up():
    """Align sandbox with schepper.yaml and start it."""
    config = load_config()
    name = config.name
    desired_ports = set(expand_ports(config.ports))
    info = get_sandbox_info(name)

    # Collect changes that require full recreation
    recreate_reasons = []
    if info:
        if memory_to_mib(config.memory) != info.get("memory_mib"):
            recreate_reasons.append(f"memory: {info.get('memory', '?')} → {config.memory}")
        if config.kit != info.get("agent"):
            recreate_reasons.append(f"kit: {info.get('agent', '?')} → {config.kit}")

    if recreate_reasons:
        click.echo("The following changes require recreating the sandbox:")
        for reason in recreate_reasons:
            click.echo(f"  • {reason}")
        click.confirm("Remove and recreate?", abort=True)
        sh(["sbx", "rm", name])
        click.echo(f"Sandbox '{name}' removed.")
        info = None

    if info is None:
        # Build create command
        cmd = ["sbx", "create", "--name", name, "--memory", config.memory]
        if config.cpus:
            cmd += ["--cpus", str(config.cpus)]
        for port in sorted(desired_ports):
            cmd += ["-p", f"{port}:{port}"]
        for resource in config.network.deny:
            cmd += ["--deny-network", resource]
        cmd += [config.kit, str(Path.cwd())]

        click.echo(f"Will create sandbox '{name}' with:")
        click.echo(f"  kit:     {config.kit}")
        click.echo(f"  memory:  {config.memory}")
        click.echo(f"  storage: {config.storage}")
        click.echo(f"  ports:   {', '.join(str(p) for p in sorted(desired_ports)) or 'none'}")
        click.echo(f"  allow:   {', '.join(config.network.allow) or 'none'}")
        click.echo(f"  deny:    {', '.join(config.network.deny) or 'none'}")
        click.confirm("Create?", abort=True)
        sh(cmd, extra_env={"DOCKER_SANDBOXES_ROOT_SIZE": config.storage})
        click.echo(f"Sandbox '{name}' created.")
        apply_network_changes(name, [("+", "allow", r) for r in config.network.allow])
    else:
        # Reconcile ports
        current_ports = get_published_ports(name)
        to_add = desired_ports - current_ports
        to_remove = current_ports - desired_ports
        changed = False

        if to_add or to_remove:
            changed = True
            click.echo(f"Port changes for '{name}':")
            for port in sorted(to_add):
                click.echo(f"  + {port}:{port}")
            for port in sorted(to_remove):
                click.echo(f"  - {port}:{port}")
            click.confirm("Apply?", abort=True)
            for port in sorted(to_add):
                r = sh(["sbx", "ports", name, "--publish", f"{port}:{port}"], check=False)
                if r.returncode != 0:
                    click.echo(f"  Warning: failed to publish {port}", err=True)
            for port in sorted(to_remove):
                r = sh(["sbx", "ports", name, "--unpublish", f"{port}:{port}"], check=False)
                if r.returncode != 0:
                    click.echo(f"  Warning: failed to unpublish {port}", err=True)

        # Reconcile network policy rules
        net_changes = network_diff(config.network, get_network_rules(name))
        if net_changes:
            changed = True
            click.echo(f"Network policy changes for '{name}':")
            for op, decision, resource in net_changes:
                click.echo(f"  {op} {decision} {resource}")
            click.confirm("Apply?", abort=True)
            apply_network_changes(name, net_changes)

        if not changed:
            click.echo(f"Sandbox '{name}' is up to date.")

    if config.ssh:
        sh(["sbx", "setup", "ssh"], check=False)

    # Start if not already running
    info = get_sandbox_info(name)
    if info and info.get("status") != "running":
        click.echo(f"Starting '{name}'…")
        sh(["sbx", "run", "--detach", "--name", name])

    click.echo(f"\nTo attach:  schepper run  (or: sbx run --name {name})")


@cli.command(name="run")
def run_cmd():
    """Attach to the sandbox (must already exist)."""
    config = load_config()
    name = config.name
    if not get_sandbox_info(name):
        click.echo(
            f"Sandbox '{name}' does not exist. Run 'schepper up' first.", err=True
        )
        sys.exit(1)
    sh(["sbx", "run", "--name", name])


@cli.command()
def status():
    """Show status of the sandbox defined in schepper.yaml."""
    config = load_config()
    name = config.name
    info = get_sandbox_info(name)
    if info is None:
        click.echo(f"{name}  not created  (run 'schepper up')")
        return
    ports = sorted(get_published_ports(name))
    workspace = (info.get("workspaces") or [""])[0]
    click.echo(f"name:      {name}")
    click.echo(f"status:    {info.get('status', '?')}")
    click.echo(f"kit:       {info.get('agent', '?')}")
    click.echo(f"memory:    {info.get('memory', '?')}")
    click.echo(f"workspace: {workspace}")
    click.echo(f"ports:     {', '.join(str(p) for p in ports) or 'none'}")
    rules = get_network_rules(name)
    click.echo(f"allow:     {', '.join(sorted(rules['allow'])) or 'none'}")
    click.echo(f"deny:      {', '.join(sorted(rules['deny'])) or 'none'}")


@cli.command()
def down():
    """Stop the sandbox defined in schepper.yaml."""
    config = load_config()
    result = sh(["sbx", "stop", config.name], check=False)
    if result.returncode != 0:
        click.echo(f"Could not stop '{config.name}' (already stopped?).", err=True)
        sys.exit(1)
    click.echo(f"Stopped '{config.name}'.")


@cli.command()
def rm():
    """Remove the sandbox defined in schepper.yaml."""
    config = load_config()
    name = config.name
    click.confirm(f"Remove sandbox '{name}'?", abort=True)
    result = sh(["sbx", "rm", name], check=False)
    if result.returncode != 0:
        click.echo(f"Could not remove '{name}'.", err=True)
        sys.exit(1)
    click.echo(f"Removed '{name}'.")


@cli.command()
@click.argument("name", required=False)
def inspect(name):
    """Show config of an existing sandbox as schepper.yaml."""
    if not name:
        config = load_config()
        name = config.name

    result = sh(["sbx", "ls", "--json"], check=False, capture=True)
    try:
        sandboxes = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        click.echo("Failed to parse sbx ls output.", err=True)
        sys.exit(1)

    sandbox = next((s for s in sandboxes if s["name"] == name), None)
    if sandbox is None:
        click.echo(f"No sandbox named '{name}'.", err=True)
        sys.exit(1)

    ports_result = sh(["sbx", "ports", name, "--json"], check=False, capture=True)
    ports = []
    try:
        for entry in json.loads(ports_result.stdout) or []:
            hp = _host_port(entry)
            if hp is not None:
                ports.append(hp)
    except (json.JSONDecodeError, TypeError):
        pass

    workspace = (sandbox.get("workspaces") or [""])[0]
    memory = sandbox.get("memory", "8g").lower().removesuffix("b")

    output = {
        "name": sandbox["name"],
        "kit": sandbox.get("agent", "claude"),
        "memory": memory,
        "cpus": 0,
        "storage": "10g",  # not exposed by sbx; adjust to match the value used at creation
        "ports": ports,
        "ssh": False,
        "network": {k: sorted(v) for k, v in get_network_rules(name).items()},
    }

    click.echo(f"# schepper.yaml — inspected from sandbox '{name}'")
    click.echo(f"# workspace: {workspace}")
    click.echo(yaml.dump(output, default_flow_style=False, sort_keys=False), nl=False)


@cli.command()
def ls():
    """List all sandboxes."""
    sh(["sbx", "ls"])


@cli.command()
def ssh():
    """Configure SSH access (runs sbx setup ssh globally)."""
    sh(["sbx", "setup", "ssh"])


def main():
    cli()


if __name__ == "__main__":
    main()
