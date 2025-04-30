#!/usr/bin/env python3
import argparse
import os
import stat
import subprocess
import sys
import tempfile

# ──────────────────────────────────────────────────────────────────────────────
# Dynamically generates the full start.sh into /tmp and runs it.
# Accepts two optional args:
#   1) path to config file (default ./reva.local.json)
#   2) branch name       (default main)
# ──────────────────────────────────────────────────────────────────────────────

START_SH = r'''#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# start.sh — unified launcher for Reva servers & services on macOS Terminal.app
#
# Usage:
#   chmod +x start.sh
#   ./start.sh             # uses ./reva.local.json, branch=main
#   ./start.sh path/to/reva.local.json dev
#
# Features:
#   0) Load KEY=VALUE lines from ./creds and export them.
#   1) Kills any lingering service ports.
#   2) Stops all “servers” via their configured stop scripts.
#   3) For each server:
#        • Writes a temp “launcher” script containing global exports,
#          cd into its path, then its start command;
#        • Opens a new Terminal tab, sets the tab title, and runs that launcher;
#        • Waits the configured timeout before moving on.
#   4) Repeats for each “service”, but the launcher also:
#        • Applies service-specific exports;
#        • Resets Git (`git reset --hard`), checks out the given branch (default “main”),
#          pulls latest, then runs its start script.
#   5) Ensures no leftover temp files conflict by removing `/tmp/launch_<name>_*.sh`.
#   6) Keeps each tab open interactively after the commands complete.
# ──────────────────────────────────────────────────────────────────────────────

# ── Parameters ─────────────────────────────────────────────────────────────────
CONFIG_FILE="${1:-reva.local.json}"
BRANCH="${2:-main}"

# ── Preconditions ───────────────────────────────────────────────────────────────
command -v jq >/dev/null 2>&1 || {
  echo "Error: This script requires 'jq'. Install with 'brew install jq'."
  exit 1
}

# ── VALIDATOR ───────────────────────────────────────────────────────────────────
validate_config() {
  # 0) Make sure the config file actually exists
  if [[ ! -f "$CONFIG_FILE" ]]; then
    # compute absolute path for clarity
    if command -v realpath >/dev/null 2>&1; then
      full_path=$(realpath "$CONFIG_FILE")
    else
      # fallback if realpath isn’t installed
      full_path="$(cd "$(dirname "$CONFIG_FILE")" && pwd)/$(basename "$CONFIG_FILE")"
    fi
    echo "❌ Config file not found: $full_path" >&2
    exit 1
  fi

  # 1) Verify overall JSON syntax
  if ! jq empty "$CONFIG_FILE" 2>/dev/null; then
    echo "❌ Invalid JSON in '$CONFIG_FILE'." >&2
    exit 1
  fi

  # ─── helper: in a raw JSON fragment, find any duplicate keys
  find_dupes_in_block() {
    local fragment="$1" keys dupes
    mapfile -t keys < <(
      printf '%s\n' "$fragment" \
        | grep -Eo '"[^"]+"[[:space:]]*:' \
        | sed -E 's/"([^"]+)".*/\1/'
    )
    mapfile -t dupes < <(printf '%s\n' "${keys[@]}" | sort | uniq -d)
    printf '%s\n' "${dupes[@]}"
  }

  # ─── 2) Extract exactly the top‐level "envars" block via brace counting
  local global_block global_dupes
  global_block=$(awk '
    /"envars"[[:space:]]*:[[:space:]]*{/ { inside=1; depth=1; next }
    inside {
      # count braces to know when the block ends
      n_open  = gsub(/{/, "{")
      n_close = gsub(/}/, "}")
      depth += n_open - n_close
      if (depth<=0) exit
      print
    }
  ' "$CONFIG_FILE")

  global_dupes=($(find_dupes_in_block "$global_block"))
  if (( ${#global_dupes[@]} )); then
    echo "❌ Duplicate global envars: ${global_dupes[*]}" >&2
    exit 1
  fi

  # ── 3) Per-service envars duplicates ────────────────────────────────────
  # correct loop over each name individually
  for svc in $(jq -r '.services[].name' "$CONFIG_FILE"); do
    # extract only that service’s envars block with brace counting
    svc_block=$(awk -v name="$svc" '
      $0 ~ "\"name\"[[:space:]]*:[[:space:]]*\"" name "\"" { found=1 }
      found && /"envars"[[:space:]]*:[[:space:]]*{/ { inside=1; depth=1; next }
      inside {
        n_open  = gsub(/{/, "{")
        n_close = gsub(/}/, "}")
        depth += n_open - n_close
        if (depth<=0) exit
        print
      }
    ' "$CONFIG_FILE")

    # now spot duplicates in that fragment
    svc_dupes=($(find_dupes_in_block "$svc_block"))
    if (( ${#svc_dupes[@]} )); then
      echo "❌ Duplicate envars in service '$svc': ${svc_dupes[*]}" >&2
      exit 1
    fi
  done

  echo "✅ Config validation passed."
}


# run the validator before anything else
validate_config

# ── 0) Load credentials from creds file ─────────────────────────────────────────
CREDS_PATH=$(jq -r '.creds.path' "$CONFIG_FILE")
# inline tilde→$HOME expansion:
CREDS_EXE="${CREDS_PATH/#\~/$HOME}"

if [[ -x "$CREDS_EXE" ]]; then
  raw_creds=$("$CREDS_EXE")
elif [[ -f "$CREDS_EXE" ]]; then
  raw_creds=$(<"$CREDS_EXE")
else
  echo "Error: cannot find or execute '$CREDS_EXE'" >&2
  exit 1
fi

# Declare an associative array
declare -A creds_map

# Populate the map with KEY=VALUE pairs
while IFS=':' read -r key val; do
  [[ -z "$key" ]] && continue
  creds_map["$key"]="$val"

done <<< "$raw_creds"

# ── Helper to expand {{cred_FOO}} via creds_map ───────────────────────────
expand_val() {
  local raw="$1"
  if [[ $raw =~ ^\{\{cred_([A-Za-z0-9_]+)\}\}$ ]]; then
    local cred_key=${BASH_REMATCH[1]}
    printf '%s' "${creds_map[$cred_key]:-}"
  else
    printf '%s' "$raw"
  fi
}

# ── Helper to clean values ───────────────────────────
clean_value() {
    local value="$1"
    echo "${value//$'\r'/}" | sed 's/^\$//'
}

# ── Helper to generate envars ───────────────────────────
generate_envars_for() {
  local jq_path="$1"; shift
  # Remaining args (if any) are keys to skip/comment
  local skip=("$@")

  while IFS=$'\t' read -r key raw; do
    val=$(expand_val "$raw")

    # clean up the value
    val=$(clean_value "$val")

    # check if key is in skip[]
    local comment=
    for k in "${skip[@]}"; do
      [[ "$k" == "$key" ]] && { comment='# '; break; }
    done

    # emit either commented or real export
    printf '%sexport %s=%q\n' "$comment" "$key" "$val"
  done < <(
    jq -r "${jq_path}
           | to_entries[]
           | \"\(.key)\t\(.value)\"" "$CONFIG_FILE"
  )
}

# ── Helpers ─────────────────────────────────────────────────────────────────────
_expand() { printf '%s\n' "${1/#\~/$HOME}"; }

open_new_tab() {
  local script_path="$1"
  local tab_title="$2"
  local esc_path esc_title

  # Escape for AppleScript
  esc_path=$(printf '%s' "$script_path" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
  esc_title=$(printf '%s' "$tab_title" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')

  osascript <<EOF >/dev/null 2>&1
tell application "Terminal"
  activate
  tell application "System Events" to keystroke "t" using command down
  delay 0.2
  set t to selected tab of the front window
  tell t
    set custom title to "$esc_title"
    do script "$esc_path" in it
  end tell
end tell
EOF
}

# ── 1) Kill any lingering service ports ─────────────────────────────────────────
echo "🛑 Killing service ports…"
for port in $(jq -r '.services[] | select(.enable==true) | .ports[]' "$CONFIG_FILE"); do
  echo "  • port $port"
  if pids=$(lsof -ti tcp:"$port"); then
    printf '%s\n' "$pids" | xargs -r kill -9
  fi
done

# ── 2) Stop all servers ────────────────────────────────────────────────────────
echo "🛑 Stopping servers…"
for name in $(jq -r '.servers[] | select(.enable==true) | .name' "$CONFIG_FILE"); do
  info=".servers[] | select(.name==\"$name\")"
  path="$(_expand "$(jq -r "$info.script.path" "$CONFIG_FILE")")"
  stop_cmd=$(jq -r "$info.script.stop" "$CONFIG_FILE")
  echo "  • $name"
  (cd "$path" && eval "$stop_cmd") 2>/dev/null || true
done

# ── 3) Start servers in new tabs ────────────────────────────────────────────────
echo "🚀 Starting servers…"
for name in $(jq -r '.servers[] | select(.enable==true) | .name' "$CONFIG_FILE"); do
  info=".servers[] | select(.name==\"$name\")"
  path="$(_expand "$(jq -r "$info.script.path" "$CONFIG_FILE")")"
  start_cmd=$(jq -r "$info.script.start" "$CONFIG_FILE")
  timeout_sec=$(jq -r "$info.timeout" "$CONFIG_FILE")

  # Remove old launchers, create a fresh one
  rm -f /tmp/launch_"$name"_*.sh
  launcher=$(mktemp /tmp/launch_"$name"_XXXX.sh)
  chmod +x "$launcher"

  cat > "$launcher" <<EOF
#!/usr/bin/env bash
set -euo pipefail

# Global exports
$(generate_envars_for '.envars')

# Change into server directory and start
cd "$path"
$start_cmd

# Keep the tab open
exec \$SHELL
EOF

  echo "  • $name → tab \"$name\" (waiting ${timeout_sec}s)…"
  open_new_tab "$launcher" "$name"
  sleep "$timeout_sec"
done

# ── 4) Start services in new tabs ───────────────────────────────────────────────
echo "🚀 Starting services on branch '$BRANCH'…"
for name in $(jq -r '.services[] | select(.enable==true) | .name' "$CONFIG_FILE"); do
  info=".services[] | select(.name==\"$name\")"
  path="$(_expand "$(jq -r "$info.script.path" "$CONFIG_FILE")")"
  start_cmd=$(jq -r "$info.script.start" "$CONFIG_FILE")
  timeout_sec=$(jq -r "$info.timeout" "$CONFIG_FILE")

  # gather the service's own envar keys into an array
  readarray -t service_keys < <(
    jq -r "$info.envars
           | keys[]" "$CONFIG_FILE"
  )

  # Remove old launchers, create a fresh one
  rm -f /tmp/launch_"$name"_*.sh
  launcher=$(mktemp /tmp/launch_"$name"_XXXX.sh)
  chmod +x "$launcher"

  cat > "$launcher" <<EOF
#!/usr/bin/env bash
set -euo pipefail

# Global exports
$(generate_envars_for '.envars' "${service_keys[@]}")


# Service-specific exports
$(generate_envars_for "$info.envars")

# Git reset, checkout, pull
cd "$path"
git reset --hard
git checkout "$BRANCH"
git pull

# Start service
$start_cmd

# Keep the tab open
exec \$SHELL
EOF

  echo "  • $name → tab \"$name\" (waiting ${timeout_sec}s)…"
  open_new_tab "$launcher" "$name"
  sleep "$timeout_sec"
done

echo "✅ All tabs launched successfully!"

# ── 5) If access is enabled, wait then open URL ───────────────────────────────
if jq -e '.access.enable==true' "$CONFIG_FILE" >/dev/null; then
  access_timeout=$(jq -r '.access.timeout' "$CONFIG_FILE")
  access_url=$(jq -r '.access.url'     "$CONFIG_FILE")

  echo "🌐 Waiting ${access_timeout}s before opening access URL…"
  sleep "$access_timeout"

  echo "🌐 Opening ${access_url} in your default browser"
  open "$access_url"
fi
'''

def main():
    parser = argparse.ArgumentParser(
        description="Generate and run the Reva start.sh from a temp file"
    )
    parser.add_argument("config_file", nargs="?", default="reva.local.json",
                        help="path to your reva.local.json")
    parser.add_argument("branch", nargs="?", default="main",
                        help="git branch to checkout for services")
    args = parser.parse_args()

    # Prepare a unique temp‐path
    tmp_dir = tempfile.gettempdir()
    script_name = f"start_reva_{os.getpid()}.sh"
    script_path = os.path.join(tmp_dir, script_name)

    # Remove old script if it somehow exists
    if os.path.exists(script_path):
        os.remove(script_path)

    # Write out the embedded start.sh
    with open(script_path, "w") as f:
        f.write(START_SH)

    # Make it executable
    st = os.stat(script_path)
    os.chmod(script_path, st.st_mode | stat.S_IEXEC)

    # Now invoke it with the two args
    try:
        subprocess.run([script_path, args.config_file, args.branch], check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ start.sh failed with exit code {e.returncode}", file=sys.stderr)
        sys.exit(e.returncode)

if __name__ == "__main__":
    main()
