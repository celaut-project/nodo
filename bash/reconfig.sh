#!/bin/bash

CONFIG_FILE="config.yaml"

# --- Prerequisite: Check if yq is installed ---
if ! command -v yq &> /dev/null; then
    echo -e "\033[1;31mError: 'yq' is not installed or not found in PATH.\033[0m"
    echo -e "\033[0;33m'yq' is required to safely read and write to the YAML configuration file.\033[0m"
    echo -e "\033[0;32mInstall it using one of the following commands:\033[0m"
    echo "  - sudo snap install yq"
    echo "  - sudo apt-get install yq (on some distributions)"
    echo "  - brew install yq (on macOS)"
    echo -e "\033[0;36mFor more info, visit: https://github.com/mikefarah/yq/\033[0m"
    exit 1
fi

# --- Check if the configuration file exists ---
if [ ! -f "$CONFIG_FILE" ]; then
    printf "\033[1;31mError: Configuration file '%s' not found in the current directory.\033[0m\n" "$CONFIG_FILE"
    exit 1
fi

# --- Color Definitions ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[1;34m'
MAGENTA='\033[1;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# --- Utility Functions (based on yq) ---

get_yaml_variable() {
    local key=$1
    yq e ".$key" "$CONFIG_FILE" || echo ""
}

update_yaml_variable() {
    local key=$1
    local new_value=$2
    yq e -i ".$key = \"$new_value\"" "$CONFIG_FILE"
}

# --- Validation Functions ---

validate_url() {
    if [[ $1 =~ ^https?://.* ]]; then return 0; else
        printf "%b\n" "${RED}   -> Invalid URL. Must start with http:// or https://${NC}"; return 1; fi
}

validate_wallet_address() {
    if [[ ${#1} -ge 30 ]]; then return 0; else
        printf "%b\n" "${RED}   -> Invalid format. At least 30 characters expected.${NC}"; return 1; fi
}

validate_reputation_id() {
    if [[ ${#1} -ge 30 ]]; then return 0; else
        printf "%b\n" "${RED}   -> Invalid format. At least 30 characters expected.${NC}"; return 1; fi
}

validate_percentage() {
    if [[ $1 =~ ^[0-9]+(\.[0-9]+)?$ ]] && (( $(echo "$1 >= 0 && $1 <= 100" | bc -l) )); then return 0; else
        printf "%b\n" "${RED}   -> Invalid percentage. Enter a number between 0 and 100.${NC}"; return 1; fi
}

validate_integer() {
    if [[ $1 =~ ^-?[0-9]+$ ]]; then return 0; else
        printf "%b\n" "${RED}   -> Invalid input. Enter an integer number.${NC}"; return 1; fi
}

validate_boolean() {
    if [[ "$1" == "true" || "$1" == "false" ]]; then return 0; else
        printf "%b\n" "${RED}   -> Invalid input. Enter 'true' or 'false'.${NC}"; return 1; fi
}

# --- Interactive Input Handler ---

handle_variable() {
    local key=$1
    local description=$2
    local validation_function=$3
    local current_value=$(get_yaml_variable "$key")

    printf "%b\n" "${MAGENTA}-----------------------------------------------------${NC}"
    printf "%b\n" "${CYAN}Configuring: ${YELLOW}${description}${NC}"

    if [[ "$current_value" == "null" || -z "$current_value" ]]; then
        printf "   Current value: ${YELLOW}(not set)${NC}\n"
    elif [[ "$key" == "ledgers.0.WALLET_MNEMONIC" || "$key" == "network.NGROK_TUNNELS_KEY" ]]; then
        printf "   Current value: ${GREEN}${current_value:0:5}...${current_value: -5}${NC}\n"
    else
        printf "   Current value: ${GREEN}${current_value}${NC}\n"
    fi

    local new_value
    while true; do
        printf "%b" "${YELLOW}   -> Enter a new value or press [Enter] to keep current: ${NC}"
        read -r new_value

        if [ -z "$new_value" ]; then
            printf "%b\n" "${CYAN}   No changes made.${NC}"
            break
        fi

        if [ -n "$validation_function" ]; then
            if $validation_function "$new_value"; then
                update_yaml_variable "$key" "$new_value"
                printf "%b\n" "${GREEN}   => Value updated successfully.${NC}"
                break
            else
                printf "%b\n" "${RED}   => Invalid input. Please try again.${NC}"
            fi
        else
            update_yaml_variable "$key" "$new_value"
            printf "%b\n" "${GREEN}   => Value updated successfully.${NC}"
            break
        fi
    done
    printf "\n"
}

# --- Main Execution ---

clear
printf "%b\n" "${BLUE}#############################################################${NC}"
printf "%b\n" "${BLUE}#${NC}             ${YELLOW}Node Configuration Utility${NC}             ${BLUE}#${NC}"
printf "%b\n" "${BLUE}#############################################################${NC}"
printf "\n"
printf "%b\n" "${CYAN}Starting interactive configuration for '$CONFIG_FILE'...${NC}"
printf "\n"

handle_variable "ledgers.ergo.NODE_URL" "Ergo Node URL" validate_url
handle_variable "ledgers.ergo.WALLET_MNEMONIC" "Ergo Wallet Mnemonic" validate_wallet_address
handle_variable "reputation.REPUTATION_PROOF_ID" "Reputation Proof ID" validate_reputation_id
handle_variable "payments.PAYMENTS_RECEIVER_WALLET" "Payment Receiver Wallet" validate_wallet_address
handle_variable "network.NGROK_TUNNELS_KEY" "NGROK Tunnels Key"
handle_variable "costs.FREE_GAS_THRESHOLD" "Free Gas Threshold" validate_integer
handle_variable "costs.SOCIALIZATION_FACTOR" "Socialization Factor" validate_integer
handle_variable "payments.DONATION_PERCENTAGE" "Donation Percentage (e.g. 5.5)" validate_percentage
handle_variable "logs.DEBUG_MODE" "Debug Mode (true/false)" validate_boolean

# --- Completion ---
printf "\n"
printf "%b\n" "${MAGENTA}-----------------------------------------------------${NC}"
printf "%b\n" "${BLUE}Configuration process completed.${NC}"
printf "\n"
printf "%b\n" "${GREEN}The file '$CONFIG_FILE' has been updated.${NC}"
printf "\n"

exit 0
