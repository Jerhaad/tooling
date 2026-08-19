#!/usr/bin/env bash
# Wrapper for schedulers that take a script name rather than a command.
exec "$(cd -P "$(dirname "${BASH_SOURCE[0]}")/../bin" && pwd)/mm-monitor" health
