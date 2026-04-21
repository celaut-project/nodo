#!/bin/bash

# --- Configuration ---
KYA_DOC_NAME="KyA.md"
KYA_DOC_RELATIVE_PATH="docs/$KYA_DOC_NAME"
FAST_GUIDE_DOC_NAME="QUICK_GUIDE.md"
FAST_GUIDE_DOC_RELATIVE_PATH="docs/$FAST_GUIDE_DOC_NAME"
ACCEPTANCE_MARKER_DIR_RELATIVE_PATH="storage"
ACCEPTANCE_MARKER_FILENAME=".acceptedkya"

display_doc() {
    local doc_file="$1"
    local doc_name="$2"

    if [[ ! -f "$doc_file" ]]; then
        echo "Error: $doc_name file not found at '$doc_file'." >&2
        exit 1
    fi

    if command -v less &> /dev/null; then
        less --quit-if-one-screen --RAW-CONTROL-CHARS "$doc_file"
    else
        cat "$doc_file"
        echo
        echo "(Info: 'less' not found, displayed with 'cat')"
    fi
}

# --- Script Logic ---
if [[ -z "$1" ]]; then
    echo "Error: No target directory provided." >&2
    echo "Usage: $0 <TARGET_DIR>" >&2
    exit 1
fi

TARGET_DIR="$(cd "$1" >/dev/null 2>&1 && pwd)"
if [[ -z "$TARGET_DIR" ]]; then
    echo "Error: Target directory '$1' not found or inaccessible." >&2
    exit 1
fi

kya_file="$TARGET_DIR/$KYA_DOC_RELATIVE_PATH"
fast_guide_file="$TARGET_DIR/$FAST_GUIDE_DOC_RELATIVE_PATH"
accepted_marker_dir="$TARGET_DIR/$ACCEPTANCE_MARKER_DIR_RELATIVE_PATH"
accepted_marker_file="$accepted_marker_dir/$ACCEPTANCE_MARKER_FILENAME"

if [[ -f "$accepted_marker_file" ]]; then
    echo "KyA already accepted for '$TARGET_DIR'."
    echo "(Marker found: '$accepted_marker_file')"
    exit 0
fi

if [[ ! -f "$kya_file" ]]; then
    echo "Error: KyA file not found at '$kya_file'." >&2
    exit 1
fi

if [[ ! -f "$fast_guide_file" ]]; then
    echo "Error: QUICK_GUIDE file not found at '$fast_guide_file'." >&2
    exit 1
fi

echo "------------------------------------------------------------"
echo " Know Your Assumptions (KyA)"
echo " Please review this document before continuing."
echo " Source: $kya_file"
echo "------------------------------------------------------------"
echo

display_doc "$kya_file" "KyA"

echo "------------------------------------------------------------"
read -r -p "Do you accept the Know Your Assumptions? (yes/no): " response

# Solo “yes” o “y” se interpretan como aceptación
if [[ "${response,,}" == "yes" || "${response,,}" == "y" ]]; then
    echo "You have accepted the Know Your Assumptions."
    mkdir -p "$accepted_marker_dir" || {
        echo "Error: Failed to create directory '$accepted_marker_dir'." >&2
        exit 1
    }
    touch "$accepted_marker_file" || {
        echo "Error: Failed to create marker file '$accepted_marker_file'." >&2
        exit 1
    }
    echo "Acceptance recorded in '$accepted_marker_file'."
    echo
    echo "------------------------------------------------------------"
    echo " KyA accepted"
    echo " Next step: quick guide for using Nodo"
    echo " Source: $fast_guide_file"
    echo "------------------------------------------------------------"
    echo
    display_doc "$fast_guide_file" "FAST_GUIDE"
    echo
    echo "KyA process completed for '$TARGET_DIR'."
    exit 0
else
    echo "KyA was not accepted. Exiting."
    exit 1
fi
