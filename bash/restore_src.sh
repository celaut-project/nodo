#!/bin/bash

# Ensure the script is run from a Git repository
if [ ! -d ".git" ]; then
  echo "Error: This script must be run from the root of a Git repository." >&2
  exit 1
fi

# Get the list of files that have been modified (both staged and unstaged)
files=$(git status --porcelain | awk '{print $2}')

# Check if there are files to restore
if [ -z "$files" ]; then
  exit 0
fi

# Restore files to their last committed state
for file in $files; do
  git restore "$file"
done
