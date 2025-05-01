#!/bin/bash

ENV_FILE=".env"

if [ ! -f "$ENV_FILE" ]; then
    # Create the file if it doesn't exist to avoid errors later, especially when adding new variables
    touch "$ENV_FILE"
    printf "\033[1;33mWarning: The .env file was not found at '$PWD/$ENV_FILE'. It has been created.\033[0m\n"
    # exit 1 # Commented out exit to allow creation and continuation
fi

# --- Color Definitions ---
# Check if tput is available and supports colors
if command -v tput >/dev/null && tput setaf 1 >/dev/null 2>&1; then
    RED=$(tput setaf 1)
    GREEN=$(tput setaf 2)
    YELLOW=$(tput setaf 3)
    BLUE=$(tput setaf 4)
    MAGENTA=$(tput setaf 5)
    CYAN=$(tput setaf 6)
    NC=$(tput sgr0) # No Color
else
    # Fallback to ANSI escape codes
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m' # Bold Yellow for emphasis
    BLUE='\033[1;34m'   # Bold Blue for titles
    MAGENTA='\033[1;35m' # Bold Magenta for separators
    CYAN='\033[0;36m'
    NC='\033[0m' # No Color
fi

# --- Utility Functions ---

# Function to safely read a variable from the .env file
get_env_variable() {
    local var_name=$1
    # Use grep to find the line starting with var_name=, then sed to remove the prefix
    # 2>/dev/null suppresses grep errors if the variable isn't found
    local value=$(grep "^${var_name}=" "$ENV_FILE" 2>/dev/null | sed -e "s/^${var_name}=//")
    echo "$value"
}

# Function to update or add a variable in the .env file
update_env_variable() {
    local var_name=$1
    local new_value=$2
    # Escape special characters (\, /, &) in the value for sed compatibility
    escaped_value=$(echo "$new_value" | sed -e 's/[\/&]/\\&/g')

    # Check if the variable already exists (case-sensitive, starts with var_name=)
    if grep -q "^${var_name}=" "$ENV_FILE" 2>/dev/null; then
        # Variable exists, update it using sed. The `|` is used as a delimiter to handle paths in values.
        sed -i.bak "s|^${var_name}=.*|${var_name}=${escaped_value}|" "$ENV_FILE"
        # Remove backup file created by sed -i on macOS compatibility (optional, uncomment if needed)
        # rm -f "${ENV_FILE}.bak"
    else
        # Variable doesn't exist, append it to the file
        echo "${var_name}=${escaped_value}" >> "$ENV_FILE"
        printf "%b\n" "${YELLOW}   -> Note: ${var_name} was not found and has been added.${NC}"
    fi
}

# --- Validation Functions ---

validate_url() {
    # Check if the input starts with http:// or https://
    if [[ $1 =~ ^https?://.* ]]; then
        return 0 # Success
    else
        printf "%b\n" "${RED}   -> Invalid URL. Must start with http:// or https://${NC}"
        return 1 # Failure
    fi
}

validate_wallet_address() {
    # Check if the input length is at least 30 characters
    if [[ ${#1} -ge 30 ]]; then
        return 0 # Success
    else
        printf "%b\n" "${RED}   -> Invalid format. Expected at least 30 characters.${NC}"
        return 1 # Failure
    fi
}

validate_reputation_id() {
    # Check if the input length is at least 30 characters (assuming similar format to wallet)
    if [[ ${#1} -ge 30 ]]; then
        return 0 # Success
    else
        printf "%b\n" "${RED}   -> Invalid format. Expected at least 30 characters.${NC}"
        return 1 # Failure
    fi
}

validate_percentage() {
    # Check if input is a number (integer or float) between 0 and 100 using bc for float comparison
    if [[ $1 =~ ^[0-9]+(\.[0-9]+)?$ ]] && (( $(echo "$1 >= 0 && $1 <= 100" | bc -l) )); then
        return 0 # Success
    else
        printf "%b\n" "${RED}   -> Invalid percentage. Enter a number between 0 and 100.${NC}"
        return 1 # Failure
    fi
}

validate_boolean() {
    # Check if input is 'true' or 'false' (case-insensitive)
    if [[ "$1" =~ ^[tT][rR][uU][eE]$ || "$1" =~ ^[fF][aA][lL][sS][eE]$ ]]; then
        return 0 # Success
    else
        printf "%b\n" "${RED}   -> Invalid input. Enter 'true' or 'false'.${NC}"
        return 1 # Failure
    fi
}

validate_integer() {
    # Check if input is an integer (allows optional leading minus sign)
    if [[ $1 =~ ^-?[0-9]+$ ]]; then
        return 0 # Success
    else
        printf "%b\n" "${RED}   -> Invalid input. Enter an integer number.${NC}"
        return 1 # Failure
    fi
}


# --- Core Logic Functions ---

# Function to handle interaction for a single variable
handle_variable() {
    local var_name=$1
    local validation_function=$2 # Optional validation function name
    local current_value=$(get_env_variable "$var_name")

    printf "%b\n" "${MAGENTA}---------------------------------${NC}"
    printf "%b\n" "${CYAN}Variable: ${YELLOW}${var_name}${NC}"

    # Check if the variable exists in the file
    if ! grep -q "^${var_name}=" "$ENV_FILE" 2>/dev/null; then
        printf "%b\n" "   Status: ${RED}Not present in ${ENV_FILE}${NC}"
    elif [ -z "$current_value" ]; then
        # Variable exists but is empty
        printf "%b\n" "   Current value: ${YELLOW}(empty)${NC}"
    else
        # Variable exists and has a value
        # Special handling for sensitive variables to show only partial value
        if [[ "$var_name" == "ERGO_WALLET_MNEMONIC" ]] || [[ "$var_name" == "NGROK_TUNNELS_KEY" ]]; then
             # Show first 5 and last 5 characters
            printf "%b\n" "   Current value: ${GREEN}${current_value:0:5}...${current_value: -5}${NC}"
        else
            # Show full value for other variables
            printf "%b\n" "   Current value: ${GREEN}${current_value}${NC}"
        fi
    fi

    # Ask user if they want to modify this variable
    local modify
    printf "%b" "${YELLOW}   Modify this variable? (y/n): ${NC}"
    read modify
    if [[ "$modify" =~ ^[yY]$ ]]; then
        local new_value=""
        while true; do # Loop until valid input is provided
            printf "%b" "${YELLOW}   -> Enter new value: ${NC}"
            # Use `read -r` to handle backslashes literally if needed, though not strictly necessary here
            read -r new_value
            # Check if a validation function was provided
            if [ -n "$validation_function" ]; then
                # Call the validation function with the new value
                if $validation_function "$new_value"; then
                    # Validation passed
                    update_env_variable "$var_name" "$new_value"
                    printf "%b\n" "${GREEN}   => ${var_name} updated successfully.${NC}"
                    break # Exit the loop
                else
                    # Validation failed
                    printf "%b\n" "${RED}   => Input invalid. Please try again.${NC}"
                    # Loop continues
                fi
            else
                # No validation function provided, accept any value
                update_env_variable "$var_name" "$new_value"
                printf "%b\n" "${GREEN}   => ${var_name} updated successfully.${NC}"
                break # Exit the loop
            fi
        done
    else
        # User chose not to modify
        printf "%b\n" "${CYAN}   Skipping modification.${NC}"
    fi
    printf "\n" # Add a newline for better spacing
}

# Function to handle the optional donation setup
handle_donation() {
    printf "%b\n" "${MAGENTA}---------------------------------${NC}"
    printf "%b\n" "${CYAN}Optional: Donation Setup${NC}"
    local donate

    printf "%b" "${YELLOW}   Donate a %% of profits to support development? (y/n): ${NC}" # Escaped %
    read donate
    if [[ "$donate" =~ ^[yY]$ ]]; then
        local donation_percentage=""
        while true; do # Loop for validation
            printf "%b" "${YELLOW}   -> Enter donation percentage (0-100): ${NC}"
            read donation_percentage
            if validate_percentage "$donation_percentage"; then
                 # Convert percentage to a decimal fraction for internal use (e.g., 10 -> 0.10)
                local internal_percentage=$(echo "scale=4; $donation_percentage / 100" | bc)
                update_env_variable "ERGO_DONATION_PERCENTAGE" "$internal_percentage"
                printf "%b\n" "${GREEN}   => Donation percentage set to ${donation_percentage}%%.${NC}" # Escaped %

                # Show thank you message if donation is greater than 0
                if (( $(echo "$donation_percentage > 0" | bc -l) )); then
                    printf "%b\n" "${GREEN}   Thank you for your support! 🙏${NC}"
                fi
                break # Exit validation loop
            else
                printf "%b\n" "${RED}   => Input invalid. Please try again.${NC}"
                # Loop continues
            fi
        done
    else
        # User opted out or entered 'n'
        update_env_variable "ERGO_DONATION_PERCENTAGE" "0" # Set donation to 0
        printf "%b\n" "${CYAN}   Donation percentage set to 0%%.${NC}" # Escaped %
    fi
    printf "\n" # Add spacing
}

# Function to display a summary of the current configuration
display_summary() {
    printf "%b\n" "${BLUE}=================== Configuration Summary ===================${NC}"
    printf "%b\n" "${CYAN}File: $PWD/$ENV_FILE${NC}"
    printf "\n"

    local max_len=0
    # List of variables to display in the summary (add new ones here)
    local vars_to_display=("ERGO_NODE_URL" "ERGO_WALLET_MNEMONIC" "REPUTATION_PROOF_ID" "ERGO_PAYMENTS_RECIVER_WALLET" "NGROK_TUNNELS_KEY" "FREE_GAS_THRESHOLD" "SOCIALIZATION_FACTOR" "ERGO_DONATION_PERCENTAGE")

    # Calculate the maximum length of variable names for alignment
    for var_name in "${vars_to_display[@]}"; do
        local display_name="$var_name"
        # Special display name for donation percentage
        if [[ "$var_name" == "ERGO_DONATION_PERCENTAGE" ]]; then
            display_name="Donation Percentage"
        fi
        # Update max_len if current display_name is longer
        [[ ${#display_name} -gt $max_len ]] && max_len=${#display_name}
    done
    ((max_len+=2)) # Add padding for alignment

    # Loop through the variables and print their status and value
    for var_name in "${vars_to_display[@]}"; do
        local value=$(get_env_variable "$var_name")
        local display_name="$var_name"
        local display_value=""
        local status_color="${RED}" # Default color is red (for not set)
        local final_string=""

        # Check if the variable line exists in the .env file
        if ! grep -q "^${var_name}=" "$ENV_FILE" 2>/dev/null; then
            display_value="Not Set"
            status_color="${RED}"
            # Handle special display name for donation if not set
            if [[ "$var_name" == "ERGO_DONATION_PERCENTAGE" ]]; then
                display_name="Donation Percentage"
            fi
            # Format the output string with alignment
            final_string=$(printf "   %-*s : %s%s%s" $max_len "$display_name" "$status_color" "$display_value" "$NC")

        # Special handling for ERGO_DONATION_PERCENTAGE display
        elif [[ "$var_name" == "ERGO_DONATION_PERCENTAGE" ]]; then
            display_name="Donation Percentage"
            if [ -z "$value" ]; then # Check if value is empty
                display_value="(empty)"
                status_color="${YELLOW}"
            else
                # Use bc to compare the fractional value with 0
                if (( $(echo "$value == 0" | bc -l) )); then
                    display_value="0%"
                    status_color="${YELLOW}" # Yellow for 0% donation
                else
                    # Convert fraction back to percentage for display (scale=2 for 2 decimal places)
                    display_value=$(echo "scale=2; $value * 100 / 1" | bc | tr -d '\n')"%"
                    status_color="${GREEN}" # Green for active donation
                fi
            fi
             # Format the output string
            final_string=$(printf "   %-*s : %s%s%s" $max_len "$display_name" "$status_color" "$display_value" "$NC")

        # Default handling for other variables
        else
            if [ -z "$value" ]; then # Check if value is empty
                display_value="(empty)"
                status_color="${YELLOW}"
            else
                # Variable has a value
                status_color="${GREEN}"
                 # Mask sensitive values
                if [[ "$var_name" == "ERGO_WALLET_MNEMONIC" ]] || [[ "$var_name" == "NGROK_TUNNELS_KEY" ]]; then
                    display_value="${value:0:5}...${value: -5}"
                else
                    # Show full value for non-sensitive variables
                    display_value="$value"
                fi
            fi
            # Format the output string
            final_string=$(printf "   %-*s : %s%s%s" $max_len "$display_name" "$status_color" "$display_value" "$NC")
        fi
        printf "%s\n" "$final_string" # Print the formatted line
    done

    printf "%b\n" "${BLUE}=============================================================${NC}"
    printf "\n"
}

# --- Main Execution ---

clear # Clear the terminal screen
printf "%b\n" "${BLUE}#############################################################${NC}"
printf "%b\n" "${BLUE}#${NC}                  ${YELLOW}Nodo Configuration Utility${NC}                 ${BLUE}#${NC}"
printf "%b\n" "${BLUE}#############################################################${NC}"
printf "\n"

# Display initial summary before modifications
display_summary
printf "%b\n" "${CYAN}Starting interactive configuration...${NC}"
printf "\n"

# Handle each configuration variable interactively
handle_variable "ERGO_NODE_URL" validate_url
handle_variable "ERGO_WALLET_MNEMONIC" validate_wallet_address # No specific validation, just presence check
handle_variable "REPUTATION_PROOF_ID" validate_reputation_id
handle_variable "ERGO_PAYMENTS_RECIVER_WALLET" validate_wallet_address
handle_variable "NGROK_TUNNELS_KEY" # No specific validation needed
handle_variable "FREE_GAS_THRESHOLD" validate_integer
handle_variable "SOCIALIZATION_FACTOR" validate_integer

# Handle donation setup separately
handle_donation

# --- Completion ---
printf "\n"
printf "%b\n" "${MAGENTA}---------------------------------${NC}"
printf "%b\n" "${BLUE}Configuration process completed.${NC}"
printf "\n"
printf "%b\n" "${CYAN}Final Configuration:${NC}"
# Display final summary after all modifications
display_summary

printf "%b\n" "${GREEN}Exiting script.${NC}"

exit 0 # Exit successfully