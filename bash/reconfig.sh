#!/bin/bash

if [ -z "${BASH_VERSION:-}" ]; then
    printf "Error: This script requires bash. Run: bash %s\n" "$0" >&2
    exit 1
fi

CONFIG_FILE="config.yaml"

# --- Prerequisite Checks ---
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
    echo -e "\033[1;31mError: Configuration file '$CONFIG_FILE' not found.\033[0m"
    exit 1
fi

# --- Color Definitions ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[1;34m'
MAGENTA='\033[1;35m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# --- Master List of All Configurable Variables ---
ALL_VARIABLES=(
    "ledgers.ergo.NODE_URL" "ledgers.ergo.WALLET_MNEMONIC"
    "reputation.REPUTATION_PROOF_ID"
    "payments.PAYMENTS_RECEIVER_WALLET" "payments.DONATION_PERCENTAGE"
    "costs.FREE_GAS_THRESHOLD" "costs.SOCIALIZATION_FACTOR" "costs.ALLOW_GAS_DEBT"
    "packer.SAVE_ALL"
    "communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS" "communication.SEND_ONLY_HASHES_ASKING_COST" "communication.DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH"
    "misc.VALIDATE_ON_IMPORT"
    "logs.DEBUG_MODE"
)
TOTAL_VARS=${#ALL_VARIABLES[@]}

# --- Utility & Validation Functions (Shared) ---
get_yaml_variable() { yq e ".$1" "$CONFIG_FILE" || echo "null"; }
update_yaml_variable() {
    local key=$1; local new_value=$2
    if [[ "$new_value" == "true" || "$new_value" == "false" ]]; then
        yq e -i ".$key = $new_value" "$CONFIG_FILE"
    else
        yq e -i ".$key = \"$new_value\"" "$CONFIG_FILE"
    fi
}
is_variable_set() {
    local value=$(get_yaml_variable "$1")
    if [[ "$value" == "null" || -z "$value" ]]; then return 1; else return 0; fi
}
validate_url() { if [[ $1 =~ ^https?://.* ]]; then return 0; else echo -e "${RED}   -> Invalid URL. Must start with http:// or https://${NC}"; return 1; fi; }
validate_wallet_address() { if [[ ${#1} -ge 30 ]]; then return 0; else echo -e "${RED}   -> Invalid format. At least 30 characters expected.${NC}"; return 1; fi; }
validate_reputation_id() { if [[ ${#1} -ge 30 ]]; then return 0; else echo -e "${RED}   -> Invalid format. At least 30 characters expected.${NC}"; return 1; fi; }
validate_percentage() { if [[ $1 =~ ^[0-9]+(\.[0-9]+)?$ ]] && (( $(echo "$1 >= 0 && $1 <= 100" | bc -l) )); then return 0; else echo -e "${RED}   -> Invalid percentage. Enter a number between 0 and 100.${NC}"; return 1; fi; }
validate_integer() { if [[ $1 =~ ^-?[0-9]+$ ]]; then return 0; else echo -e "${RED}   -> Invalid input. Enter an integer number.${NC}"; return 1; fi; }
validate_boolean() { if [[ "$1" == "true" || "$1" == "false" ]]; then return 0; else echo -e "${RED}   -> Invalid input. Enter 'true' or 'false'.${NC}"; return 1; fi; }

# --- Interactive Input Handler (Shared) ---
handle_variable() {
    local key=$1; local description=$2; local validation_function=$3
    local current_value=$(get_yaml_variable "$key")
    echo -e "${MAGENTA}-----------------------------------------------------${NC}"
    echo -e "${CYAN}Configuring: ${YELLOW}${description}${NC}"
    if ! is_variable_set "$key"; then
        echo -e "   Current value: ${YELLOW}(not set)${NC}"
    elif [[ "$key" == *MNEMONIC* || "$key" == *KEY* ]]; then
        echo -e "   Current value: ${GREEN}${current_value:0:5}...${current_value: -5}${NC}"
    else
        echo -e "   Current value: ${GREEN}${current_value}${NC}"
    fi
    local new_value
    while true; do
        echo -n -e "${YELLOW}   -> Enter a new value or press [Enter] to keep current: ${NC}"
        read -r new_value
        if [ -z "$new_value" ]; then echo -e "${CYAN}   No changes made.${NC}"; break; fi
        if [ -n "$validation_function" ]; then
            if $validation_function "$new_value"; then
                update_yaml_variable "$key" "$new_value"; echo -e "${GREEN}   => Value updated successfully.${NC}"; break
            else
                echo -e "${RED}   => Invalid input. Please try again.${NC}"
            fi
        else
            update_yaml_variable "$key" "$new_value"; echo -e "${GREEN}   => Value updated successfully.${NC}"; break
        fi
    done; echo ""
}

# --- MODE 1: Quick Setup (Simplified Version) ---
run_quick_setup() {
    clear
    echo -e "${BLUE}#############################################################${NC}"
    echo -e "${BLUE}#${NC}              ${YELLOW}Quick Setup Utility${NC}                 ${BLUE}#${NC}"
    echo -e "${BLUE}#${NC}          (Configuring essential variables)          ${BLUE}#${NC}"
    echo -e "${BLUE}#############################################################${NC}\n"
    echo -e "${CYAN}Starting interactive configuration for '$CONFIG_FILE'...${NC}\n"
    handle_variable "ledgers.ergo.NODE_URL" "Ergo Node URL" validate_url
    handle_variable "ledgers.ergo.WALLET_MNEMONIC" "Ergo Wallet Mnemonic" validate_wallet_address
    handle_variable "reputation.REPUTATION_PROOF_ID" "Reputation Proof ID" validate_reputation_id
    handle_variable "payments.PAYMENTS_RECEIVER_WALLET" "Payment Receiver Wallet" validate_wallet_address
    handle_variable "payments.DONATION_PERCENTAGE" "Donation Percentage (e.g. 5.5)" validate_percentage
    echo -e "\n${GREEN}Quick setup complete!${NC}"
    echo -n "Press [Enter] to return to the main menu."
    read -r
}

# --- MODE 2: Advanced Setup (Categorized Version) ---
configure_ledgers() { handle_variable "ledgers.ergo.NODE_URL" "Ergo Node URL" validate_url; handle_variable "ledgers.ergo.WALLET_MNEMONIC" "Ergo Wallet Mnemonic" validate_wallet_address; }
configure_reputation() { handle_variable "reputation.REPUTATION_PROOF_ID" "Reputation Proof ID" validate_reputation_id; }
configure_payments() { handle_variable "payments.PAYMENTS_RECEIVER_WALLET" "Payment Receiver Wallet" validate_wallet_address; handle_variable "payments.DONATION_PERCENTAGE" "Donation Percentage (e.g., 5.5)" validate_percentage; }
configure_costs() { handle_variable "costs.FREE_GAS_THRESHOLD" "Free Gas Threshold" validate_integer; handle_variable "costs.SOCIALIZATION_FACTOR" "Socialization Factor" validate_integer; handle_variable "costs.ALLOW_GAS_DEBT" "Allow Gas Debt (true/false)" validate_boolean; }
configure_packer() { handle_variable "packer.SAVE_ALL" "Packer: Save all items (true/false)" validate_boolean; }
configure_communication() { handle_variable "communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS" "Comm: Announce instance to connecting peers"; handle_variable "communication.SEND_ONLY_HASHES_ASKING_COST" "Comm: Send only hashes when asking for cost"; handle_variable "communication.DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH" "Comm: Deny cost request if hash is not available"; }
configure_misc() { handle_variable "misc.VALIDATE_ON_IMPORT" "Misc: Validate on import (true/false)" validate_boolean; }
configure_logs() { handle_variable "logs.DEBUG_MODE" "Debug Mode (true/false)" validate_boolean; }

run_advanced_setup() {
    while true; do
        clear
        echo -e "${BLUE}#############################################################${NC}"
        echo -e "${BLUE}#${NC}           ${YELLOW}Advanced Configuration Utility${NC}            ${BLUE}#${NC}"
        echo -e "${BLUE}#############################################################${NC}"
        local set_count=0
        for var in "${ALL_VARIABLES[@]}"; do if is_variable_set "$var"; then ((set_count++)); fi; done
        local status_color="${YELLOW}"; if [ "$set_count" -eq "$TOTAL_VARS" ]; then status_color="${GREEN}"; fi
        echo -e "\n${status_color}Configuration Status: ${set_count} of ${TOTAL_VARS} variables are set.${NC}\n"
        get_category_status() { local vars=("$@"); local total=${#vars[@]}; local count=0; for var in "${vars[@]}"; do if is_variable_set "$var"; then ((count++)); fi; done; if [ "$count" -eq "$total" ]; then echo -e "${GREEN}($count/$total set)${NC}"; else echo -e "${YELLOW}($count/$total set)${NC}"; fi; }
        echo -e "Select a category to configure:"
        echo -e " 1) Ledgers        $(get_category_status "ledgers.ergo.NODE_URL" "ledgers.ergo.WALLET_MNEMONIC")"
        echo -e " 2) Reputation     $(get_category_status "reputation.REPUTATION_PROOF_ID")"
        echo -e " 3) Payments       $(get_category_status "payments.PAYMENTS_RECEIVER_WALLET" "payments.DONATION_PERCENTAGE")"
        echo -e " 5) Costs          $(get_category_status "costs.FREE_GAS_THRESHOLD" "costs.SOCIALIZATION_FACTOR" "costs.ALLOW_GAS_DEBT")"
        echo -e " 6) Packer         $(get_category_status "packer.SAVE_ALL")"
        echo -e " 7) Communication  $(get_category_status "communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS" "communication.SEND_ONLY_HASHES_ASKING_COST" "communication.DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH")"
        echo -e " 8) Misc           $(get_category_status "misc.VALIDATE_ON_IMPORT")"
        echo -e " 9) Logs           $(get_category_status "logs.DEBUG_MODE")"
        echo -e "-----------------------------------------------------"
        if [ "$set_count" -ne "$TOTAL_VARS" ]; then echo -e "${CYAN}10) Configure ALL unset variables...${NC}"; fi
        echo -e " 0) Back to Main Menu\n"
        echo -n -e "${YELLOW}Choose an option: ${NC}"; read -r choice
        case $choice in
            1) configure_ledgers ;; 2) configure_reputation ;; 3) configure_payments ;; 4) configure_network ;; 5) configure_costs ;; 6) configure_packer ;; 7) configure_communication ;; 8) configure_misc ;; 9) configure_logs ;;
            10) if [ "$set_count" -ne "$TOTAL_VARS" ]; then
                    echo -e "\n${CYAN}Reviewing all configuration categories...${NC}"
                    configure_ledgers; configure_reputation; configure_payments; configure_network; configure_costs; configure_packer; configure_communication; configure_misc; configure_logs
                    echo -e "\n${GREEN}Full review completed.${NC}"; sleep 2
                else echo -e "\n${GREEN}All variables are already set. Nothing to do.${NC}"; sleep 2; fi ;;
            0) break ;; *) echo -e "\n${RED}Invalid option. Please try again.${NC}"; sleep 1 ;;
        esac
    done
}

# --- MODE 3: View All Configuration (REVISED) ---
print_kv() {
    local description=$1; local key=$2; local value=$(get_yaml_variable "$key")
    local padded_desc=$(printf '%-45s' "$description")
    echo -n -e "  ${CYAN}${padded_desc}${NC}"
    if ! is_variable_set "$key"; then
        echo -e "${YELLOW}(not set)${NC}"
    elif [[ "$key" == *MNEMONIC* || "$key" == *KEY* ]]; then
        echo -e "${GREEN}${value:0:5}...${value: -5}${NC}"
    elif [[ "$value" == "true" || "$value" == "false" ]]; then
        echo -e "${MAGENTA}${value}${NC}"
    else
        echo -e "${GREEN}${value}${NC}"
    fi
}

view_all_variables() {
    clear
    echo -e "${BLUE}#############################################################${NC}"
    echo -e "${BLUE}#${NC}             ${YELLOW}Current Configuration Viewer${NC}             ${BLUE}#${NC}"
    echo -e "${BLUE}#############################################################${NC}\n"

    echo -e "${BOLD}--- LEDGERS ---${NC}"
    print_kv "Node URL" "ledgers.ergo.NODE_URL"
    print_kv "Wallet Mnemonic" "ledgers.ergo.WALLET_MNEMONIC"
    
    echo -e "\n${BOLD}--- REPUTATION ---${NC}"
    print_kv "Reputation Proof ID" "reputation.REPUTATION_PROOF_ID"

    echo -e "\n${BOLD}--- PAYMENTS ---${NC}"
    print_kv "Payment Receiver Wallet" "payments.PAYMENTS_RECEIVER_WALLET"
    print_kv "Donation Percentage" "payments.DONATION_PERCENTAGE"

    echo -e "\n${BOLD}--- COSTS ---${NC}"
    print_kv "Free Gas Threshold" "costs.FREE_GAS_THRESHOLD"
    print_kv "Socialization Factor" "costs.SOCIALIZATION_FACTOR"
    print_kv "Allow Gas Debt" "costs.ALLOW_GAS_DEBT"

    echo -e "\n${BOLD}--- PACKER ---${NC}"
    print_kv "Save All Items" "packer.SAVE_ALL"

    echo -e "\n${BOLD}--- COMMUNICATION ---${NC}"
    print_kv "Announce to Connecting Peers" "communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS"
    print_kv "Send Only Hashes for Cost" "communication.SEND_ONLY_HASHES_ASKING_COST"
    print_kv "Deny Cost Request if Hash Unavailable" "communication.DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH"
    
    echo -e "\n${BOLD}--- MISC ---${NC}"
    print_kv "Validate on Import" "misc.VALIDATE_ON_IMPORT"
    
    echo -e "\n${BOLD}--- LOGS ---${NC}"
    print_kv "Debug Mode" "logs.DEBUG_MODE"

    echo -e "\n\n${GREEN}End of configuration list.${NC}"
    echo -n "Press [Enter] to return to the main menu."
    read -r
}


# --- Main Execution: Top-Level Menu ---
while true; do
    clear
    echo -e "${BLUE}#############################################################${NC}"
    echo -e "${BLUE}#${NC}             ${YELLOW}Node Configuration Utility${NC}              ${BLUE}#${NC}"
    echo -e "${BLUE}#############################################################${NC}"
    echo -e "\nWelcome! Please choose an option:\n"
    echo -e " ${YELLOW}1)${NC} Quick Setup (Recommended for first-time use)"
    echo -e " ${YELLOW}2)${NC} Advanced Configuration (All options by category)"
    echo -e " ${YELLOW}3)${NC} View Current Configuration\n"
    echo -e " ${YELLOW}0)${NC} Exit\n"
    echo -n -e "${YELLOW}Choose an option: ${NC}"
    read -r main_choice
    case $main_choice in
        1) run_quick_setup ;;
        2) run_advanced_setup ;;
        3) view_all_variables ;;
        0) break ;;
        *) echo -e "\n${RED}Invalid option. Please try again.${NC}"; sleep 1 ;;
    esac
done

# --- Completion ---
echo -e "\n${MAGENTA}-----------------------------------------------------${NC}"
echo -e "${BLUE}Exiting configuration utility.${NC}\n"
echo -e "${GREEN}The file '$CONFIG_FILE' has been updated with your changes.${NC}\n"
exit 0
