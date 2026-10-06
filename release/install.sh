#!/usr/bin/env bash
# Install Mine Monitor from this release bundle, or upgrade an existing installation to it.
#
#   sudo ./install.sh                  # interactive: asks for the address operators will use
#   sudo ./install.sh --help           # every option
#
# Instructions: README.md in this directory (deploy/INSTALL.md in the repository).
set -euo pipefail
exec bash "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/deploy/mmctl" install "$@"
