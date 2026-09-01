#!/usr/bin/env bash
# The macOS/Linux twin of HWE_Scaled.cmd.
cd "$(dirname "$0")" && exec python3 hwe_scaled_ui.py "$@"
