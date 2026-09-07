# StateFork environment configuration — sample.
#
# Usage:
#     cp env.sample.sh env.local.sh
#     # edit env.local.sh
#     source ./env.local.sh        # before running StateFork
#
# env.local.sh is gitignored. Every variable below is OPTIONAL. StateFork
# only resolves the waypoint launcher; Waypoint resolves its own runtime
# settings from environment variables, config files, then built-in defaults.

# --- Waypoint backend ---

# Path to the `waypoint` binary. Unset -> ./waypoint symlink in the repo root, then $PATH.
# export WAYPOINT_BIN=/abs/path/to/waypoint

# Path to the `bash_init` helper. Unset -> Waypoint config/default.
# export WAYPOINT_BASH_INIT_SRC=/abs/path/to/bash_init

# Directory where Waypoint stores session state (OverlayFS upper + CRIU images).
# Unset -> Waypoint config/default.
# export WAYPOINT_SESSIONS_DIR=/var/lib/statefork/sessions

# Keep session directories after cleanup. Unset -> Waypoint config/default.
# export WAYPOINT_PRESERVE_SESSION_ON_CLEANUP=true
