#!/bin/bash

# Check if the target directory is provided
if [ -z "$1" ]; then
    echo "Usage: $0 TARGET_DIR"
    echo "Example: $0 /nodo"
    exit 1
fi

# Set target directory from the first argument
TARGET_DIR="$1"

# Define paths
ENV_FILE="$TARGET_DIR/.env"
ENV_EXAMPLE_FILE="$TARGET_DIR/.env.example"

# Check if .env file exists
if [ ! -f "$ENV_FILE" ]; then
    echo ".env file does not exist. Copying .env.example to .env..."
    if [ -f "$ENV_EXAMPLE_FILE" ]; then
        cp "$ENV_EXAMPLE_FILE" "$ENV_FILE"
        echo ".env.example has been copied to .env."
    else
        echo "Error: .env.example file does not exist in $TARGET_DIR."
        exit 1
    fi
else
    echo ".env file already exists. Updating .env to match .env.example..."
    # Create a temporary file to store the updated .env content
    TEMP_ENV_FILE=$(mktemp)

    # Read lines with context from .env.example to keep comments and variables
    grep -v -E '^$|^\s*#.*' "$ENV_EXAMPLE_FILE" | while IFS= read -r line; do
        # Extract variable name from the line
        var_name=$(echo "$line" | cut -d '=' -f 1)
        
        # Retrieve the comment line if present
        comment=$(grep -B 1 "^$var_name=" "$ENV_EXAMPLE_FILE" | grep -E '^\s*#')

        # Check if the variable exists in .env
        if grep -q "^$var_name=" "$ENV_FILE"; then
            # Append the comment if present
            if [ ! -z "$comment" ]; then
                echo "$comment" >> "$TEMP_ENV_FILE"
            fi
            # Keep the current value from .env
            grep "^$var_name=" "$ENV_FILE" >> "$TEMP_ENV_FILE"
        else
            # Append the comment if present
            if [ ! -z "$comment" ]; then
                echo "$comment" >> "$TEMP_ENV_FILE"
            fi
            # Add the new variable from .env.example
            echo "$line" >> "$TEMP_ENV_FILE"
        fi
    done

    # Process .env to find variables that need to be removed
    grep -v -E '^$|^\s*#.*' "$ENV_FILE" | while IFS= read -r line; do
        var_name=$(echo "$line" | cut -d '=' -f 1)
        # Check if the variable does not exist in .env.example
        if ! grep -q "^$var_name=" "$ENV_EXAMPLE_FILE"; then
            echo "Warning: $var_name is in .env but not in .env.example. Removing from .env."
        fi
    done

    # Replace the original .env with the updated content
    mv "$TEMP_ENV_FILE" "$ENV_FILE"
    echo ".env has been updated to match .env.example with comments."
fi
