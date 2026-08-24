#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${1:-/opt/astap}"
DATA_DIR="${2:-/usr/share/astap/data}"

mkdir -p "$DATA_DIR"

# ASTAP's Debian database packages install their payload under /opt/astap,
# while the command-line application searches /usr/share/astap/data. Link
# each top-level package entry so databases added by separate packages are
# all discoverable without moving package-owned files.
if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "ASTAP source directory $SOURCE_DIR does not exist; nothing to link."
    exit 0
fi

while IFS= read -r -d '' source_path; do
    entry_name="$(basename "$source_path")"
    destination="$DATA_DIR/$entry_name"

    if [[ -L "$destination" ]]; then
        current_target="$(readlink "$destination")"
        if [[ "$current_target" == "$source_path" ]]; then
            continue
        fi

        ln -sfnT "$source_path" "$destination"
        echo "Repaired ASTAP data link: $destination -> $source_path"
    elif [[ -e "$destination" ]]; then
        echo "Keeping existing ASTAP data entry: $destination"
    else
        ln -s "$source_path" "$destination"
        echo "Created ASTAP data link: $destination -> $source_path"
    fi
done < <(find "$SOURCE_DIR" -mindepth 1 -maxdepth 1 -print0)
