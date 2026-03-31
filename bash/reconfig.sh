#!/bin/bash

if [ -z "${BASH_VERSION:-}" ]; then
    printf "Error: This script requires bash. Run: bash %s\n" "$0" >&2
    exit 1
fi

CONFIG_FILE="config.yaml"

# --- Prerequisite Checks ---
if ! command -v yq &> /dev/null; then
    echo -e "\033[1;31mError: 'yq' is not installed.\033[0m"
    exit 1
fi
if ! command -v bc &> /dev/null; then
    echo -e "\033[1;31mError: 'bc' is not installed.\033[0m"
    exit 1
fi

# --- Check config file ---
if [ ! -f "$CONFIG_FILE" ]; then
    echo -e "\033[1;31mError: Configuration file '$CONFIG_FILE' not found.\033[0m"
    exit 1
fi

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[1;34m'
MAGENTA='\033[1;35m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# --- Variables ---
ALL_VARIABLES=(
    "ledgers.ergo.NODE_URL" "ledgers.ergo.WALLET_MNEMONIC"
    "reputation.REPUTATION_PROOF_ID"
    "payments.PAYMENTS_RECEIVER_WALLET" "payments.DONATION_PERCENTAGE"
    "publisher.REPOSITORY" "publisher.TOKEN"
)

TOTAL_VARS=${#ALL_VARIABLES[@]}

# --- YAML Helpers ---
get_yaml_variable() { yq e ".$1" "$CONFIG_FILE" || echo "null"; }

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
    [[ "$value" != "null" && -n "$value" ]]
}

# --- Validators ---
validate_url() { [[ $1 =~ ^https?://.* ]]; }
validate_wallet() { [[ ${#1} -ge 30 ]]; }
validate_repo() { [[ $1 =~ ^[^/]+/[^/]+$ ]]; }
validate_token() { [[ ${#1} -ge 10 ]]; }

# --- Input handler ---
handle_variable() {
    local key=$1
    local description=$2
    local validator=$3

    local current=$(get_yaml_variable "$key")

    echo -e "\n${CYAN}$description${NC}"
    echo -e "Current: ${GREEN}${current}${NC}"

    while true; do
        echo -n "New value (Enter = keep): "
        read -r val

        [ -z "$val" ] && break

        if [ -n "$validator" ] && ! $validator "$val"; then
            echo -e "${RED}Invalid value${NC}"
            continue
        fi

        update_yaml_variable "$key" "$val"
        echo -e "${GREEN}Updated${NC}"
        break
    done
}

# --- Ensure publisher defaults exist ---
init_publisher_defaults() {
yq e -i '
.publisher.PROVIDER = "github" |
.publisher.BRANCH = "main" |
.publisher.SOURCE_APPLICATION_WEB_PAGE = "https://reputation-systems.github.io/source-application?tab=add" |
.publisher.TOKEN_ENV_VAR = "CLOUD_TOKEN" |
.publisher.FALLBACK_TOKEN = "" |
.publisher.FALLBACK_TOKEN_ENV_VAR = "GITHUB_TOKEN" |
.publisher.CHUNK_SIZE_MB = 24 |
.publisher.UPLOADS_PREFIX = "uploads" |
.publisher.TIMEOUT_SECONDS = 300 |
.publisher.MAX_RETRY = 3 |
.publisher.BACKOFF_SECONDS = 2 |
.publisher.KEEP_DOWNLOADED_FILE = true |
.publisher.AUTO_IMPORT_SERVICE_ON_DOWNLOAD = true
' "$CONFIG_FILE"
}

# --- Quick Setup ---
run_quick_setup() {
    clear
    echo -e "${BLUE}Quick Setup${NC}"

    handle_variable "ledgers.ergo.NODE_URL" "Node URL" validate_url
    handle_variable "ledgers.ergo.WALLET_MNEMONIC" "Wallet Mnemonic" validate_wallet
    handle_variable "reputation.REPUTATION_PROOF_ID" "Reputation ID" validate_wallet
    handle_variable "payments.PAYMENTS_RECEIVER_WALLET" "Receiver Wallet" validate_wallet
    handle_variable "payments.DONATION_PERCENTAGE" "Donation %" 

    echo -e "\n${MAGENTA}--- Publisher Setup ---${NC}"
    init_publisher_defaults
    handle_variable "publisher.REPOSITORY" "GitHub Repository (owner/repo)" validate_repo
    handle_variable "publisher.TOKEN" "GitHub Token" validate_token

    echo -e "\n${GREEN}Quick setup complete${NC}"
    read -r
}

# --- Advanced ---
configure_publisher() {
    init_publisher_defaults
    handle_variable "publisher.REPOSITORY" "Repository (owner/repo)" validate_repo
    handle_variable "publisher.TOKEN" "GitHub Token" validate_token
}

run_advanced_setup() {
    while true; do
        clear
        echo -e "${BLUE}Advanced Setup${NC}"

        echo "1) Ledgers"
        echo "2) Reputation"
        echo "3) Payments"
        echo "4) Publisher"
        echo "0) Back"

        read -r c

        case $c in
            1)
                handle_variable "ledgers.ergo.NODE_URL" "Node URL" validate_url
                handle_variable "ledgers.ergo.WALLET_MNEMONIC" "Mnemonic" validate_wallet
                ;;
            2)
                handle_variable "reputation.REPUTATION_PROOF_ID" "Reputation ID" validate_wallet
                ;;
            3)
                handle_variable "payments.PAYMENTS_RECEIVER_WALLET" "Wallet" validate_wallet
                handle_variable "payments.DONATION_PERCENTAGE" "Donation %"
                ;;
            4)
                configure_publisher
                ;;
            0) break ;;
        esac
    done
}

# --- View ---
view_all() {
    clear
    echo -e "${BLUE}Current Config${NC}"

    for var in "${ALL_VARIABLES[@]}"; do
        echo "$var = $(get_yaml_variable "$var")"
    done

    read -r
}

# --- Main ---
while true; do
    clear
    echo "1) Quick Setup"
    echo "2) Advanced"
    echo "3) View"
    echo "0) Exit"

    read -r opt

    case $opt in
        1) run_quick_setup ;;
        2) run_advanced_setup ;;
        3) view_all ;;
        0) break ;;
    esac
done

echo "Done."
exit 0
