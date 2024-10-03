#!/bin/bash

# Path to the .env file
ENV_FILE=".env"

# Check if the .env file exists
if [ ! -f "$ENV_FILE" ]; then
    echo "The .env file was not found."
    exit 1
fi

# Colors for better visibility (works in most terminals)
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[1;34m'
NC='\033[0m' # No Color

# Function to read a variable from the .env file
get_env_variable() {
    local var_name=$1
    local value=$(grep -oP "^${var_name}=\K.*" "$ENV_FILE")
    echo "$value"
}

# Function to update a variable in the .env file, handling special characters
update_env_variable() {
    local var_name=$1
    local new_value=$2
    # Escape special characters that might cause issues in sed (/, &, etc.)
    escaped_value=$(echo "$new_value" | sed 's/[&/\]/\\&/g')
    sed -i "s|^${var_name}=.*|${var_name}=${escaped_value}|" "$ENV_FILE"
}

# Function to validate URLs
validate_url() {
    if [[ $1 =~ ^https?://.* ]]; then
        return 0
    else
        return 1
    fi
}

# Function to validate the wallet address format (simple length validation)
validate_wallet_address() {
    if [[ ${#1} -ge 30 ]]; then
        return 0
    else
        return 1
    fi
}

# Function to validate donation percentage (should be 0 to 100)
validate_percentage() {
    if [[ $1 =~ ^[0-9]+(\.[0-9]+)?$ ]] && (( $(echo "$1 <= 100" | bc -l) )); then
        return 0
    else
        return 1
    fi
}

# Function to display and prompt for a variable
handle_variable() {
    local var_name=$1
    local validation_function=$2
    local current_value=$(get_env_variable "$var_name")

    echo -e "${BLUE}---------------------------------${NC}"
    echo -e "${YELLOW}Variable: ${var_name}${NC}"

    if [ -z "$current_value" ]; then
        echo -e "${RED}$var_name does not have any assigned value.${NC}"
    else
        echo -e "Current value of ${GREEN}$var_name${NC}: ${current_value}"
    fi

    read -p "Do you want to modify $var_name? (y/n): " modify
    if [[ "$modify" =~ ^[yY]$ ]]; then
        local new_value=""
        while true; do
            read -p "Enter the new value for $var_name: " new_value
            if [ -z "$validation_function" ] || $validation_function "$new_value"; then
                update_env_variable "$var_name" "$new_value"
                echo -e "${GREEN}$var_name successfully updated.${NC}"
                break
            else
                echo -e "${RED}Invalid value. Please try again.${NC}"
            fi
        done
    fi
}

# Function to ask for voluntary donation
handle_donation() {
    echo -e "${BLUE}---------------------------------${NC}"
    echo -e "${YELLOW}Donation Setup${NC}"
    read -p "Do you want to donate a percentage of profits to support node development? (y/n): " donate
    if [[ "$donate" =~ ^[yY]$ ]]; then
        # Handle donation percentage only
        local donation_percentage=""
        while true; do
            read -p "Enter the donation percentage (0-100): " donation_percentage
            if validate_percentage "$donation_percentage"; then
                # Convert percentage from 0-100 to 0-1 before saving
                donation_percentage=$(echo "scale=4; $donation_percentage / 100" | bc)
                update_env_variable "ERGO_DONATION_PERCENTAGE" "$donation_percentage"
                echo -e "${GREEN}Donation percentage successfully updated to $donation_percentage (stored as 0-1 scale).${NC}"

                if (( $(echo "$donation_percentage > 0" | bc -l) )); then
                    echo -e "${YELLOW}Thank you for your donation!${NC}"
                fi
                break
            else
                echo -e "${RED}Invalid percentage. Please enter a value between 0 and 100.${NC}"
            fi
        done
    else
        echo -e "${YELLOW}Skipping donation setup.${NC}"
    fi
}

# Function to display summary before processing
display_summary() {
    echo -e "${BLUE}=================================${NC}"
    echo -e "${YELLOW}Summary of current configuration:${NC}"

    for var_name in "ERGO_NODE_URL" "ERGO_WALLET_MNEMONIC" "ERGO_PAYMENTS_RECIVER_WALLET" "NGROK_TUNNELS_KEY"; do
        local value=$(get_env_variable "$var_name")
        if [ -z "$value" ]; then
            echo -e "${RED}$var_name: Not Set${NC}"
        else
            echo -e "${GREEN}$var_name${NC}: $value"
        fi
    done

    # Add donation percentage to the summary
    local donation_percentage=$(get_env_variable "ERGO_DONATION_PERCENTAGE")
    if [ -z "$donation_percentage" ] || (( $(echo "$donation_percentage == 0" | bc -l) )); then
        echo -e "${YELLOW}Donation: ${RED}Not Set or 0%${NC}"
    else
        # Convert from 0-1 scale to 0-100 for display
        local display_percentage=$(echo "$donation_percentage * 100" | bc)
        echo -e "${YELLOW}Donation: ${GREEN}$display_percentage%${NC}"
    fi

    echo -e "${BLUE}=================================${NC}"
}

# Main script execution
echo -e "${BLUE}Welcome to the Node Configuration Script${NC}"
display_summary

# Manage the main variables
handle_variable "ERGO_NODE_URL" validate_url
handle_variable "ERGO_WALLET_MNEMONIC" validate_wallet_address
handle_variable "ERGO_PAYMENTS_RECIVER_WALLET" validate_wallet_address
handle_variable "NGROK_TUNNELS_KEY"  # No validation function needed here

# Handle optional donation
handle_donation

echo -e "${BLUE}Process completed.${NC}"
