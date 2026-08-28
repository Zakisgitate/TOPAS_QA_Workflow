#!/bin/zsh
set -e
case_root="${0:A:h}"
cd "$case_root"
exec "$case_root/.venv/bin/python" "$case_root/launch_gui.py"
