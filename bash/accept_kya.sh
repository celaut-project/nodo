#!/bin/bash

# --- Configuration ---
# Name of the KyA document
KYA_DOC_NAME="KyA.md"
# Relative path to the KyA document within the target directory
KYA_DOC_RELATIVE_PATH="docs/$KYA_DOC_NAME"
# Relative path to the directory where the acceptance marker will be stored
ACCEPTANCE_MARKER_DIR_RELATIVE_PATH="storage" # Changed 'storate' to 'storage' - adjust if needed
# Name of the acceptance marker file (hidden file)
ACCEPTANCE_MARKER_FILENAME=".acceptedkya"

# --- Script Logic ---

# Check if the target directory parameter is provided
if [[ -z "$1" ]]; then
    echo "Error: No target directory provided." >&2
    echo "Usage: $0 <TARGET_DIR>" >&2
    exit 1
fi

# Assign the first parameter to the TARGET_DIR variable
# Resolve to an absolute path for clarity, though not strictly necessary
TARGET_DIR="$(cd "$1" >/dev/null 2>&1 && pwd)"
if [[ -z "$TARGET_DIR" ]]; then
    echo "Error: Target directory '$1' not found or inaccessible." >&2
    exit 1
fi


# Define the full path to the kya.md file and the acceptance marker
kya_file="$TARGET_DIR/$KYA_DOC_RELATIVE_PATH"
accepted_marker_dir="$TARGET_DIR/$ACCEPTANCE_MARKER_DIR_RELATIVE_PATH"
accepted_marker_file="$accepted_marker_dir/$ACCEPTANCE_MARKER_FILENAME"

# --- Check for existing acceptance ---
if [[ -f "$accepted_marker_file" ]]; then
    echo "KyA already accepted for '$TARGET_DIR'."
    echo "(Marker found: '$accepted_marker_file')"
    exit 0
fi

# --- Check if the KyA file exists ---
if [[ ! -f "$kya_file" ]]; then
    echo "Error: KyA file not found at '$kya_file'." >&2
    exit 1
fi

# --- Present the KyA ---
echo "------------------------------------------------------------"
echo " Please review the 'Know Your Assumptions' (KyA)"
echo " Source: $kya_file"
echo "------------------------------------------------------------"
echo # Blank line for spacing

# Use 'less' for pagination if available, otherwise 'cat'
if command -v less &> /dev/null; then
    less --quit-if-one-screen --RAW-CONTROL-CHARS "$kya_file"
else
    cat "$kya_file"
    echo # Blank line if cat was used
    echo "(Info: 'less' command not found, displayed content using 'cat')"
fi

echo "------------------------------------------------------------"

# --- Ask for acceptance ---
while true; do
    # Use -p for prompt, -r to handle backslashes literally
    read -p "Do you accept the Know Your Assumptions? (yes/no): " response

    # Convert response to lowercase for case-insensitive comparison
    response_lower=$(echo "$response" | tr '[:upper:]' '[:lower:]')

    case "$response_lower" in
        yes|y)
            echo "You have accepted the Know Your Assumptions."

            # Create the storage directory if it doesn't exist
            mkdir -p "$accepted_marker_dir"
            if [[ $? -ne 0 ]]; then
                 echo "Error: Failed to create directory '$accepted_marker_dir'." >&2
                 # Decide if script should continue or exit. Exiting is safer.
                 exit 1
            fi

            # Create the empty marker file
            touch "$accepted_marker_file"
             if [[ $? -ne 0 ]]; then
                 echo "Error: Failed to create acceptance marker file '$accepted_marker_file'." >&2
                 # Decide if script should continue or exit. Exiting is safer.
                 exit 1
            fi

            echo "Acceptance recorded in '$accepted_marker_file'."
            break # Exit the loop
            ;;
        no|n)
            echo "You have declined the Know Your Assumptions."
            echo "Deleting target directory: $TARGET_DIR..."
            # Make sure TARGET_DIR is not empty or '/' for safety, although rm -rf on / usually requires --no-preserve-root
             if [[ -z "$TARGET_DIR" || "$TARGET_DIR" == "/" ]]; then
                 echo "Error: Safety check failed. TARGET_DIR is empty or root ('/'). Aborting deletion." >&2
                 exit 1
             fi
            rm -rf "$TARGET_DIR"
            if [[ $? -eq 0 ]]; then
                echo "Directory deleted successfully."
                # Exiting with 0 because the requested action (deletion) completed.
                exit 0
            else
                echo "Error: Failed to delete directory '$TARGET_DIR'." >&2
                # Exit with error because deletion failed
                exit 1
            fi
            ;;
        *)
            echo "Invalid input '$response'. Please enter 'yes' or 'no'."
            # Loop continues to ask again
            ;;
    esac
done

# If we reached here, it means 'yes' was chosen and the marker was created.
echo "KyA process completed for '$TARGET_DIR'."
exit 0