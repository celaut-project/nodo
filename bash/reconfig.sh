#!/bin/bash

CONFIG_FILE="config.yaml"

# --- Prerequisite: Check if yq and bc are installed ---
if ! command -v yq &> /dev/null; then
    echo -e "\033[1;31mError: 'yq' is not installed or not found in PATH.\033[0m"
    echo -e "\033[0;33m'yq' is required to safely read and write to the YAML configuration file.\033[0m"
    echo -e "\033[0;36mFor more info, visit: https://github.com/mikefarah/yq/\033[0m"
    exit 1
fi

if ! command -v bc &> /dev/null; then
    echo -e "\033[1;31mError: 'bc' is not installed or not found in PATH.\033[0m"
    echo -e "\033[0;33m'bc' is required for floating-point number validations.\033[0m"
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

# --- Master List of All Configurable Variables ---
# Keeping this list updated is key for the progress counter.
ALL_VARIABLES=(
    "ledgers.ergo.NODE_URL" "ledgers.ergo.WALLET_MNEMONIC"
    "reputation.REPUTATION_PROOF_ID"
    "payments.PAYMENTS_RECEIVER_WALLET" "payments.DONATION_PERCENTAGE"
    "network.NGROK_TUNNELS_KEY"
    "costs.FREE_GAS_THRESHOLD" "costs.SOCIALIZATION_FACTOR" "costs.ALLOW_GAS_DEBT"
    "packer.SAVE_ALL"
    "communication.SEND_INSTANCE" "communication.SEND_ONLY_HASHES_ASKING_COST" "communication.DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH"
    "misc.VALIDATE_ON_IMPORT"
    "logs.DEBUG_MODE"
)
TOTAL_VARS=${#ALL_VARIABLES[@]}

# --- Utility Functions (yq) ---
get_yaml_variable() {
    yq e ".$1" "$CONFIG_FILE" || echo "null"
}

update_yaml_variable() {
    local key=$1
    local new_value=$2
    if [[ "$new_value" == "true" || "$new_value" == "false" ]]; then
        yq e -i ".$key = $new_value" "$CONFIG_FILE"
    else
        yq e -i ".$key = \"$new_value\"" "$CONFIG_FILE"
    fi
}

is_variable_set() {
    local value=$(get_yaml_variable "$1")
    if [[ "$value" == "null" || -z "$value" ]]; then
        return 1 # False (not set)
    else
        return 0 # True (is set)
    fi
}

# --- Validation Functions ---
validate_url() { if [[ $1 =~ ^https?://.* ]]; then return 0; else printf "%b\n" "${RED}   -> Invalid URL. Must start with http:// or https://${NC}"; return 1; fi; }
validate_wallet_address() { if [[ ${#1} -ge 30 ]]; then return 0; else printf "%b\n" "${RED}   -> Invalid format. At least 30 characters expected.${NC}"; return 1; fi; }
validate_reputation_id() { if [[ ${#1} -ge 30 ]]; then return 0; else printf "%b\n" "${RED}   -> Invalid format. At least 30 characters expected.${NC}"; return 1; fi; }
validate_percentage() { if [[ $1 =~ ^[0-9]+(\.[0-9]+)?$ ]] && (( $(echo "$1 >= 0 && $1 <= 100" | bc -l) )); then return 0; else printf "%b\n" "${RED}   -> Invalid percentage. Enter a number between 0 and 100.${NC}"; return 1; fi; }
validate_integer() { if [[ $1 =~ ^-?[0-9]+$ ]]; then return 0; else printf "%b\n" "${RED}   -> Invalid input. Enter an integer number.${NC}"; return 1; fi; }
validate_boolean() { if [[ "$1" == "true" || "$1" == "false" ]]; then return 0; else printf "%b\n" "${RED}   -> Invalid input. Enter 'true' or 'false'.${NC}"; return 1; fi; }

# --- Interactive Input Handler ---
handle_variable() {
    local key=$1
    local description=$2
    local validation_function=$3
    local current_value=$(get_yaml_variable "$key")
    printf "%b\n" "${MAGENTA}-----------------------------------------------------${NC}"
    printf "%b\n" "${CYAN}Configuring: ${YELLOW}${description}${NC}"
    if ! is_variable_set "$key"; then
        printf "   Current value: ${YELLOW}(not set)${NC}\n"
    elif [[ "$key" == "ledgers.ergo.WALLET_MNEMONIC" || "$key" == "network.NGROK_TUNNELS_KEY" ]]; then
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
    done; printf "\n"
}

# --- Functions for Each Configuration Category ---
configure_ledgers() {
    handle_variable "ledgers.ergo.NODE_URL" "Ergo Node URL" validate_url
    handle_variable "ledgers.ergo.WALLET_MNEMONIC" "Ergo Wallet Mnemonic" validate_wallet_address
}
configure_reputation() {
    handle_variable "reputation.REPUTATION_PROOF_ID" "Reputation Proof ID" validate_reputation_id
}
configure_payments() {
    handle_variable "payments.PAYMENTS_RECEIVER_WALLET" "Payment Receiver Wallet" validate_wallet_address
    handle_variable "payments.DONATION_PERCENTAGE" "Donation Percentage (e.g., 5.5)" validate_percentage
}
configure_network() {
    handle_variable "network.NGROK_TUNNELS_KEY" "NGROK Tunnels Key"
}
configure_costs() {
    handle_variable "costs.FREE_GAS_THRESHOLD" "Free Gas Threshold" validate_integer
    handle_variable "costs.SOCIALIZATION_FACTOR" "Socialization Factor" validate_integer
    handle_variable "costs.ALLOW_GAS_DEBT" "Allow Gas Debt (true/false)" validate_boolean
}
configure_packer() {
    handle_variable "packer.SAVE_ALL" "Packer: Save all items (true/false)" validate_boolean
}
configure_communication() {
    handle_variable "communication.SEND_INSTANCE" "Communication: Announce instance to connecting peers"
    handle_variable "communication.SEND_ONLY_HASHES_ASKING_COST" "Communication: Send only hashes when asking for cost"
    handle_variable "communication.DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH" "Communication: Deny cost request if hash is not available"
}
configure_misc() {
    handle_variable "misc.VALIDATE_ON_IMPORT" "Misc: Validate on import (true/false)" validate_boolean
}
configure_logs() {
    handle_variable "logs.DEBUG_MODE" "Debug Mode (true/false)" validate_boolean
}

# --- Main Menu Logic ---
while true; do
    clear
    printf "%b\n" "${BLUE}#############################################################${NC}"
    printf "%b\n" "${BLUE}#${NC}             ${YELLOW}Node Configuration Utility${NC}              ${BLUE}#${NC}"
    printf "%b\n" "${BLUE}#############################################################${NC}"

    # --- Calculate and display configuration status ---
    set_count=0
    for var in "${ALL_VARIABLES[@]}"; do
        if is_variable_set "$var"; then
            ((set_count++))
        fi
    done
    
    status_color="${YELLOW}"
    if [ "$set_count" -eq "$TOTAL_VARS" ]; then
        status_color="${GREEN}"
    fi
    printf "\n${status_color}Configuration Status: ${set_count} of ${TOTAL_VARS} variables are set.${NC}\n\n"
    
    # Function to get the status of a category
    get_category_status() {
        local vars=("$@")
        local total=${#vars[@]}
        local count=0
        for var in "${vars[@]}"; do
            if is_variable_set "$var"; then
                ((count++))
            fi
        done
        if [ "$count" -eq "$total" ]; then
            echo -e "${GREEN}($count/$total set)${NC}"
        else
            echo -e "${YELLOW}($count/$total set)${NC}"
        fi
    }
    
    # Menu Options
    cat_ledgers_vars=("ledgers.ergo.NODE_URL" "ledgers.ergo.WALLET_MNEMONIC")
    cat_reputation_vars=("reputation.REPUTATION_PROOF_ID")
    cat_payments_vars=("payments.PAYMENTS_RECEIVER_WALLET" "payments.DONATION_PERCENTAGE")
    cat_network_vars=("network.NGROK_TUNNELS_KEY")
    cat_costs_vars=("costs.FREE_GAS_THRESHOLD" "costs.SOCIALIZATION_FACTOR" "costs.ALLOW_GAS_DEBT")
    cat_packer_vars=("packer.SAVE_ALL")
    cat_comm_vars=("communication.SEND_INSTANCE" "communication.SEND_ONLY_HASHES_ASKING_COST" "communication.DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH")
    cat_misc_vars=("misc.VALIDATE_ON_IMPORT")
    cat_logs_vars=("logs.DEBUG_MODE")

    printf "Select a category to configure:\n"
    printf " 1) Ledgers        %s\n" "$(get_category_status "${cat_ledgers_vars[@]}")"
    printf " 2) Reputation     %s\n" "$(get_category_status "${cat_reputation_vars[@]}")"
    printf " 3) Payments       %s\n" "$(get_category_status "${cat_payments_vars[@]}")"
    printf " 4) Network        %s\n" "$(get_category_status "${cat_network_vars[@]}")"
    printf " 5) Costs          %s\n" "$(get_category_status "${cat_costs_vars[@]}")"
    printf " 6) Packer         %s\n" "$(get_category_status "${cat_packer_vars[@]}")"
    printf " 7) Communication  %s\n" "$(get_category_status "${cat_comm_vars[@]}")"
    printf " 8) Misc           %s\n" "$(get_category_status "${cat_misc_vars[@]}")"
    printf " 9) Logs           %s\n" "$(get_category_status "${cat_logs_vars[@]}")"
    printf -- "-----------------------------------------------------\n"
    if [ "$set_count" -ne "$TOTAL_VARS" ]; then
        printf "${CYAN}10) Configure ALL unset variables...${NC}\n"
    fi
    printf " 0) Exit\n\n"

    printf "${YELLOW}Choose an option: ${NC}"
    read -r choice

    case $choice in
        1) configure_ledgers ;;
        2) configure_reputation ;;
        3) configure_payments ;;
        4) configure_network ;;
        5) configure_costs ;;
        6) configure_packer ;;
        7) configure_communication ;;
        8) configure_misc ;;
        9) configure_logs ;;
        10) 
            if [ "$set_count" -ne "$TOTAL_VARS" ]; then
                printf "\n${CYAN}Reviewing all configuration categories...${NC}\n\n"
                configure_ledgers
                configure_reputation
                configure_payments
                configure_network
                configure_costs
                configure_packer
                configure_communication
                configure_misc
                configure_logs
                printf "${GREEN}Full review completed.${NC}\n"
                sleep 2
            else
                printf "\n${GREEN}All variables are already set. Nothing to do.${NC}\n"; sleep 2
            fi
            ;;
        0) break ;;
        *) printf "\n${RED}Invalid option. Please try again.${NC}\n"; sleep 1 ;;
    esac
done

# --- Completion ---
printf "\n"
printf "%b\n" "${MAGENTA}-----------------------------------------------------${NC}"
printf "%b\n" "${BLUE}Configuration process completed.${NC}"
printf "\n"
printf "%b\n" "${GREEN}The file '$CONFIG_FILE' has been updated.${NC}"
printf "\n"

exit 0