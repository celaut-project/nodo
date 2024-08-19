#!/bin/bash

# Check if the target directory is provided as an argument
if [ -z "$1" ]; then
    echo "Usage: $0 TARGET_DIR"
    echo "Example: $0 /nodo"
    exit 1
fi

# Set the target directory from the first argument
TARGET_DIR="$1"

# Ensure the target directory exists
if [ ! -d "$TARGET_DIR" ]; then
    echo "Error: Target directory '$TARGET_DIR' does not exist." >&2
    exit 1
fi

# Ensure the target directory contains a Git repository
if [ ! -d "$TARGET_DIR/.git" ]; then
    echo "Error: Target directory '$TARGET_DIR' does not contain a Git repository." >&2
    exit 1
fi

# Change to the target directory
cd "$TARGET_DIR" || exit 1

# Get the list of files that have been modified (both staged and unstaged)
# `git status --porcelain` gives a short output format with status and file path
# `awk '{print $2}'` extracts the file path from this output
files=$(git status --porcelain | awk '{print $2}')

# Check if there are any files to restore
if [ -z "$files" ]; then
  exit 0
fi

# Restore each modified file to its last committed state
for file in $files; do
  git restore "$file"
done
