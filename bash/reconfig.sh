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
    "ledgers.ergo.reputation.REPUTATION_PROOF_ID"
    "ledgers.ergo.payments.COLD_WALLET" "ledgers.ergo.payments.HOT_WALLET_LIMITS"
    "ledgers.ergo.payments.COLD_WALLET_MIN_TRANSFER" "ledgers.ergo.payments.DONATION_PERCENTAGE"
    "publisher.REPOSITORY" "publisher.TOKEN"
    "packer.local" "packer.PACKER_SERVICE_URL" "packer.PACKER_SOURCE_URL"
    "logs.DEBUG_MODE" "logs.MEMORY_LOGS"
    "low_demand.ENABLED" "low_demand.CPU_MAX_PERCENT" "low_demand.MEM_MAX_PERCENT"
    "low_demand.POLL_INTERVAL" "low_demand.LOW_DEMAND_CONSECUTIVE_POLLS"
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

# Numeric assignment: write the value unquoted so it stays a YAML number
# (int/float) instead of a string. Callers must validate the value first.
update_yaml_number() {
    local key=$1
    local new_value=$2
    yq e -i ".$key = $new_value" "$CONFIG_FILE"
}

# The packer service id is NOT a plain key: it lives in the top-level
# `core_services` list as the `{name: "packer", id: ...}` entry (single source of
# truth). Read/update that specific list element by name.
get_packer_id() {
    local id
    id=$(yq e '.core_services[] | select(.name == "packer") | .id' "$CONFIG_FILE" 2>/dev/null)
    [[ -z "$id" || "$id" == "null" ]] && echo "null" || echo "$id"
}

set_packer_id() {
    local val=$1
    if [[ "$(yq e '.core_services[] | select(.name == "packer") | .name' "$CONFIG_FILE" 2>/dev/null)" == "packer" ]]; then
        VAL="$val" yq e -i '(.core_services[] | select(.name == "packer") | .id) = strenv(VAL)' "$CONFIG_FILE"
    else
        # No packer entry yet — create one (core_services may not exist at all).
        VAL="$val" yq e -i '.core_services = (.core_services // []) + [{"name": "packer", "id": strenv(VAL)}]' "$CONFIG_FILE"
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
validate_bool() { [[ $1 == "true" || $1 == "false" ]]; }
validate_percent() { [[ $1 =~ ^[0-9]+$ ]] && [ "$1" -ge 0 ] && [ "$1" -le 100 ]; }
validate_pos_int() { [[ $1 =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ]; }
# ERG decimal string (non-negative), e.g. "100" or "0.5".
validate_erg() { [[ $1 =~ ^[0-9]+(\.[0-9]+)?$ ]]; }
# Packer service id: a 64-char hex content hash (sha256).
validate_service_id() { [[ $1 =~ ^[0-9a-fA-F]{64}$ ]]; }

# --- Input handler ---
handle_variable() {
    local key=$1
    local description=$2
    local validator=$3
    local value_type=${4:-string}   # string | number

    local current=$(get_yaml_variable "$key")

    echo -e "\n${CYAN}$description${NC}"
    echo -e "Current: ${GREEN}${current}${NC}"

    local prompt="New value (Enter = keep): "
    [ "$value_type" = "opturl" ] && prompt="New value (Enter = keep, '-' = clear): "

    while true; do
        echo -n "$prompt"
        read -r val

        [ -z "$val" ] && break

        # Optional fields: '-' clears the value to an empty string.
        if [ "$value_type" = "opturl" ] && [ "$val" = "-" ]; then
            update_yaml_variable "$key" ""
            echo -e "${GREEN}Cleared${NC}"
            break
        fi

        if [ -n "$validator" ] && ! $validator "$val"; then
            echo -e "${RED}Invalid value${NC}"
            continue
        fi

        if [ "$value_type" = "number" ]; then
            update_yaml_number "$key" "$val"
        else
            update_yaml_variable "$key" "$val"
        fi
        echo -e "${GREEN}Updated${NC}"
        break
    done
}

# Packer service id is a list element, not a plain key — handle it separately.
handle_packer_id() {
    local current=$(get_packer_id)
    echo -e "\n${CYAN}Packer service id (64-hex content hash; core_services entry)${NC}"
    echo -e "Current: ${GREEN}${current}${NC}"
    while true; do
        echo -n "New value (Enter = keep): "
        read -r val
        [ -z "$val" ] && break
        if ! validate_service_id "$val"; then
            echo -e "${RED}Invalid service id (need 64 hex chars)${NC}"
            continue
        fi
        set_packer_id "$val"
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
    handle_variable "ledgers.ergo.reputation.REPUTATION_PROOF_ID" "Reputation ID" validate_wallet
    handle_variable "ledgers.ergo.payments.COLD_WALLET" "Cold Wallet (public address, Enter = keep, '-' = clear)" validate_wallet opturl
    handle_variable "ledgers.ergo.payments.HOT_WALLET_LIMITS" "Hot wallet limit (ERG)" validate_erg
    handle_variable "ledgers.ergo.payments.COLD_WALLET_MIN_TRANSFER" "Cold-wallet minimum transfer (ERG)" validate_erg
    handle_variable "ledgers.ergo.payments.DONATION_PERCENTAGE" "Donation %"

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

configure_packer() {
    echo -e "\n${MAGENTA}--- Packer ---${NC}"
    handle_variable "packer.local" \
        "Local Docker packer? true = build locally, false = use a packer-service" \
        validate_bool
    handle_variable "packer.PACKER_SERVICE_URL" \
        "Out-of-band packer-service base URL (e.g. http://ip:port)" \
        validate_url opturl
    handle_variable "packer.PACKER_SOURCE_URL" \
        "Packer source manifest URL (direct download)" \
        validate_url opturl
    handle_packer_id
}

configure_logs() {
    echo -e "\n${MAGENTA}--- Logs ---${NC}"
    handle_variable "logs.DEBUG_MODE" "Debug mode (true/false)" validate_bool
    handle_variable "logs.MEMORY_LOGS" "Memory logs (true/false)" validate_bool
}

configure_low_demand() {
    echo -e "\n${MAGENTA}--- Low-demand fallback ---${NC}"
    handle_variable "low_demand.ENABLED" "Enabled (true/false)" validate_bool
    handle_variable "low_demand.CPU_MAX_PERCENT" "CPU max percent (0-100)" validate_percent number
    handle_variable "low_demand.MEM_MAX_PERCENT" "Memory max percent (0-100)" validate_percent number
    handle_variable "low_demand.POLL_INTERVAL" "Poll interval, seconds (>= 1)" validate_pos_int number
    handle_variable "low_demand.LOW_DEMAND_CONSECUTIVE_POLLS" "Consecutive idle polls before start (>= 1)" validate_pos_int number
}

run_advanced_setup() {
    while true; do
        clear
        echo -e "${BLUE}Advanced Setup${NC}"

        echo "1) Ledgers"
        echo "2) Reputation"
        echo "3) Payments"
        echo "4) Publisher"
        echo "5) Packer"
        echo "6) Logs"
        echo "7) Low-demand fallback"
        echo "0) Back"

        read -r c

        case $c in
            1)
                handle_variable "ledgers.ergo.NODE_URL" "Node URL" validate_url
                handle_variable "ledgers.ergo.WALLET_MNEMONIC" "Mnemonic" validate_wallet
                ;;
            2)
                handle_variable "ledgers.ergo.reputation.REPUTATION_PROOF_ID" "Reputation ID" validate_wallet
                ;;
            3)
                handle_variable "ledgers.ergo.payments.COLD_WALLET" "Cold Wallet (public address, Enter = keep, '-' = clear)" validate_wallet opturl
                handle_variable "ledgers.ergo.payments.HOT_WALLET_LIMITS" "Hot wallet limit (ERG)" validate_erg
                handle_variable "ledgers.ergo.payments.COLD_WALLET_MIN_TRANSFER" "Cold-wallet minimum transfer (ERG)" validate_erg
                handle_variable "ledgers.ergo.payments.DONATION_PERCENTAGE" "Donation %"
                ;;
            4)
                configure_publisher
                ;;
            5)
                configure_packer
                ;;
            6)
                configure_logs
                ;;
            7)
                configure_low_demand
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
    echo "packer.id (core_services) = $(get_packer_id)"

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
