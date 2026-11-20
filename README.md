# schepper

Declarative manager for [Docker Sandboxes (sbx)](https://docs.docker.com/ai/sandboxes/).

Each project gets a `schepper.yaml`. Run `schepper up` to create or reconcile the sandbox to match.

Notice that Docker itself is working on [a similar feature](https://docs.docker.com/reference/cli/sbx/env/).

## Install

```sh
uv tool install schepper
```

## Workflow

```sh
cd ~/Projects/myproject
schepper init      # creates schepper.yaml with defaults
# edit schepper.yaml
schepper up        # creates sandbox, confirms, then prints the attach command
schepper run       # attach interactively
```

## Commands

| Command | Description |
|---|---|
| `schepper init` | Scaffold `schepper.yaml` in current project dir |
| `schepper up` | Align sandbox with config and start it; confirms all changes, warns before recreation |
| `schepper run` | Attach to the sandbox interactively (must already exist) |
| `schepper down` | Stop sandbox |
| `schepper rm` | Remove sandbox (with confirmation) |
| `schepper status` | Show status, kit, memory, ports for the current sandbox |
| `schepper inspect [name]` | Print a live sandbox's config as `schepper.yaml` (copy-pasteable) |
| `schepper ls` | List all sandboxes (`sbx ls`) |
| `schepper ssh` | Run `sbx setup ssh` globally (once) |

## schepper.yaml

```yaml
name: myproject
kit: claude
memory: 8g
cpus: 0
storage: 10g
ports:
  - 3000
  - 8000-8010
ssh: false
network:
  allow:
    - api.example.com
    - "*.pypi.org:443"
  deny:
    - tracker.example.com
```

| Field | Default | Description |
|---|---|---|
| `name` | current directory name | Sandbox name passed to `sbx` |
| `kit` | `claude` | Agent kit: `claude`, `codex`, `shell`, etc. |
| `memory` | `8g` | Memory limit in binary units, e.g. `512m`, `4g`, `8g` |
| `cpus` | `0` | CPU count; `0` = auto (all host CPUs) |
| `storage` | `10g` | VM root disk size. Cannot be changed on an existing sandbox — requires `schepper rm && schepper up`. |
| `ports` | `[]` | Ports to publish. Ranges like `8000-8010` are expanded. |
| `ssh` | `false` | When `true`, runs `sbx setup ssh` on every `schepper up` |
| `network.allow` | `[]` | Hosts this sandbox may reach, on top of the global `sbx` policy. One pattern per entry (`example.com`, `*.example.com`, `example.com:443`, CIDRs). |
| `network.deny` | `[]` | Hosts this sandbox may never reach; deny wins over any allow, including global ones. |

Ports are always mapped symmetrically (`3000:3000`), so `localhost:3000` works identically inside and outside the sandbox — Claude can suggest a preview URL and it just works.

Network rules are applied as sandbox-scoped `sbx policy` rules (`sbx policy allow|deny network --sandbox <name>`). `schepper up` adds missing rules and removes rules that are no longer listed, after confirmation. Only rules added by hand are managed — the global defaults and the rules a kit provisions are left alone. Use `sbx policy log <name>` to see which hosts were blocked.

## Notes

- **Changing `memory` or `kit`** on an existing sandbox requires recreation. `schepper up` will detect the diff, warn, and ask to confirm before removing and recreating. Changing `storage` also requires recreation, but `schepper up` cannot detect it automatically — run `schepper rm && schepper up` manually.
- **SSH**: `schepper ssh` (or `ssh: true` in config) runs `sbx setup ssh`, which writes an `~/.ssh/config` entry so `ssh <name>.sbx` connects without further setup.

## Development

```sh
git clone https://github.com/Ecno92/schepper
cd schepper
uv sync --extra dev
uv run pytest tests/ -v
```

Switch the installed `schepper` command between versions with [just](https://github.com/casey/just):

```sh
just install-dev   # editable install from this checkout
just install       # latest release from PyPI
just uninstall     # remove it completely
```

To publish a new release:

```sh
uv build
uv publish
```
