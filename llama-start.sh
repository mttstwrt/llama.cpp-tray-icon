#!/bin/sh
# Start the llama.cpp router and its tray icon.
#
# Plain POSIX sh, so it runs the same from bash, zsh, fish or dash. It is only
# a convenience wrapper: llama_tray.py creates its own virtualenv, starts the
# router and writes the logs, so `python3 llama_tray.py --start` does exactly
# the same thing.
set -eu

dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Uncomment to reach the router from other machines on the network. NOTE: with
# no --api-key and the default CORS '*', that lets anything that can reach this
# host drive the router and any MCP tools it was started with.
# LLAMA_ARG_HOST=0.0.0.0; export LLAMA_ARG_HOST

exec python3 "$dir/llama_tray.py" --start "$@"
