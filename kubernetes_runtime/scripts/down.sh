#!/usr/bin/env bash
# Delete the kind cluster and everything in it.
set -euo pipefail
kind delete cluster --name agent-runtime
