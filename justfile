# List available recipes
default:
    @just --list

# Install schepper as a uv tool from this checkout (editable: code changes apply immediately)
install-dev:
    uv tool install --force --editable "{{justfile_directory()}}"

# Install the latest released schepper from PyPI (replaces any dev install)
install:
    uv tool install --force --reinstall schepper

# Remove the schepper uv tool completely
uninstall:
    uv tool uninstall schepper
