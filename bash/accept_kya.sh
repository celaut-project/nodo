#!/bin/bash

# Check if the first parameter is provided
if [[ -z "$1" ]]; then
    echo "Error: No target directory provided."
    echo "Usage: $0 <TARGET_DIR>"
    exit 1
fi

# Assign the first parameter to the TARGET_DIR variable
TARGET_DIR="$1"

# Define the path to the kya.md file and the target directory using $TARGET_DIR
file="$TARGET_DIR/docs/kya.md"
directory="$TARGET_DIR"

# Check if the kya.md file exists
if [[ -f "$file" ]]; then
    # Display the content of kya.md
    cat "$file"

    # Ask the user if they accept the Know your assumptions
    echo -n "Do you accept the Know your assumptions? (yes/no): "
    read response

    # If the answer is 'no', delete the $TARGET_DIR directory
    if [[ "$response" == "no" ]]; then
        echo "Deleting directory $directory..."
        rm -rf "$directory"
        echo "Directory deleted."
    else
        echo "You have accepted the Know your assumptions."
    fi
else
    echo "The file $file does not exist."
fi
