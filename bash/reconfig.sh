#!/bin/bash

ENV_FILE=".env"

if [ ! -f "$ENV_FILE" ]; then
    printf "\033[0;31mError: The .env file was not found at '$PWD/$ENV_FILE'.\033[0m\n"
    exit 1
fi

if command -v tput >/dev/null && tput setaf 1 >/dev/null 2>&1; then
    RED=$(tput setaf 1)
    GREEN=$(tput setaf 2)
    YELLOW=$(tput setaf 3)
    BLUE=$(tput setaf 4)
    MAGENTA=$(tput setaf 5)
    CYAN=$(tput setaf 6)
    NC=$(tput sgr0)
else
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[1;34m'
    MAGENTA='\033[1;35m'
    CYAN='\033[0;36m'
    NC='\033[0m'
fi

get_env_variable() {
    local var_name=$1
    local value=$(grep "^${var_name}=" "$ENV_FILE" 2>/dev/null | sed -e "s/^${var_name}=//")
    echo "$value"
}

update_env_variable() {
    local var_name=$1
    local new_value=$2
    escaped_value=$(echo "$new_value" | sed -e 's/[\/&]/\\&/g')

    if grep -q "^${var_name}=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^${var_name}=.*|${var_name}=${escaped_value}|" "$ENV_FILE"
    else
        echo "${var_name}=${escaped_value}" >> "$ENV_FILE"
        printf "%b\n" "${YELLOW}   -> Note: ${var_name} was not found and has been added.${NC}"
    fi
}

validate_url() {
    if [[ $1 =~ ^https?://.* ]]; then
        return 0
    else
        printf "%b\n" "${RED}   -> Invalid URL. Must start with http:// or https://${NC}"
        return 1
    fi
}

validate_wallet_address() {
    if [[ ${#1} -ge 30 ]]; then
        return 0
    else
        printf "%b\n" "${RED}   -> Invalid format. Expected at least 30 characters.${NC}"
        return 1
    fi
}

validate_reputation_id() {
    if [[ ${#1} -ge 30 ]]; then
        return 0
    else
        printf "%b\n" "${RED}   -> Invalid format. Expected at least 30 characters.${NC}"
        return 1
    fi
}

validate_percentage() {
    if [[ $1 =~ ^[0-9]+(\.[0-9]+)?$ ]] && (( $(echo "$1 >= 0 && $1 <= 100" | bc -l) )); then
        return 0
    else
        printf "%b\n" "${RED}   -> Invalid percentage. Enter a number between 0 and 100.${NC}"
        return 1
    fi
}

handle_variable() {
    local var_name=$1
    local validation_function=$2
    local current_value=$(get_env_variable "$var_name")

    printf "%b\n" "${MAGENTA}---------------------------------${NC}"
    printf "%b\n" "${CYAN}Variable: ${YELLOW}${var_name}${NC}"

    if ! grep -q "^${var_name}=" "$ENV_FILE" 2>/dev/null; then
        printf "%b\n" "   Status: ${RED}Not present in ${ENV_FILE}${NC}"
    elif [ -z "$current_value" ]; then
        printf "%b\n" "   Current value: ${YELLOW}(empty)${NC}"
    else
        if [[ "$var_name" == "ERGO_WALLET_MNEMONIC" ]] || [[ "$var_name" == "NGROK_TUNNELS_KEY" ]]; then
            printf "%b\n" "   Current value: ${GREEN}${current_value:0:5}...${current_value: -5}${NC}"
        else
            printf "%b\n" "   Current value: ${GREEN}${current_value}${NC}"
        fi
    fi

    local modify
    printf "%b" "${YELLOW}   Modify this variable? (y/n): ${NC}"
    read modify
    if [[ "$modify" =~ ^[yY]$ ]]; then
        local new_value=""
        while true; do
            printf "%b" "${YELLOW}   -> Enter new value: ${NC}"
            read new_value
            if [ -n "$validation_function" ]; then
                if $validation_function "$new_value"; then
                    update_env_variable "$var_name" "$new_value"
                    printf "%b\n" "${GREEN}   => ${var_name} updated successfully.${NC}"
                    break
                else
                    printf "%b\n" "${RED}   => Input invalid. Please try again.${NC}"
                fi
            else
                update_env_variable "$var_name" "$new_value"
                printf "%b\n" "${GREEN}   => ${var_name} updated successfully.${NC}"
                break
            fi
        done
    else
        printf "%b\n" "${CYAN}   Skipping modification.${NC}"
    fi
    printf "\n"
}

handle_donation() {
    printf "%b\n" "${MAGENTA}---------------------------------${NC}"
    printf "%b\n" "${CYAN}Optional: Donation Setup${NC}"
    local donate

    printf "%b" "${YELLOW}   Donate a % of profits to support development? (y/n): ${NC}"
    read donate
    if [[ "$donate" =~ ^[yY]$ ]]; then
        local donation_percentage=""
        while true; do
            printf "%b" "${YELLOW}   -> Enter donation percentage (0-100): ${NC}"
            read donation_percentage
            if validate_percentage "$donation_percentage"; then
                local internal_percentage=$(echo "scale=4; $donation_percentage / 100" | bc)
                update_env_variable "ERGO_DONATION_PERCENTAGE" "$internal_percentage"
                printf "%b\n" "${GREEN}   => Donation percentage set to ${donation_percentage}%.${NC}"

                if (( $(echo "$donation_percentage > 0" | bc -l) )); then
                    printf "%b\n" "${GREEN}   Thank you for your support! 🙏${NC}"
                fi
                break
            else
                printf "%b\n" "${RED}   => Input invalid. Please try again.${NC}"
            fi
        done
    else
        update_env_variable "ERGO_DONATION_PERCENTAGE" "0"
        printf "%b\n" "${CYAN}   Donation percentage set to 0%.${NC}"
    fi
    printf "\n"
}

display_summary() {
    printf "%b\n" "${BLUE}=================== Configuration Summary ===================${NC}"
    printf "%b\n" "${CYAN}File: $PWD/$ENV_FILE${NC}"
    printf "\n"

    local max_len=0
    local vars_to_display=("ERGO_NODE_URL" "ERGO_WALLET_MNEMONIC" "REPUTATION_PROOF_ID" "ERGO_PAYMENTS_RECIVER_WALLET" "NGROK_TUNNELS_KEY" "ERGO_DONATION_PERCENTAGE")

    for var_name in "${vars_to_display[@]}"; do
        local display_name="$var_name"
        if [[ "$var_name" == "ERGO_DONATION_PERCENTAGE" ]]; then
            display_name="Donation Percentage"
        fi
        [[ ${#display_name} -gt $max_len ]] && max_len=${#display_name}
    done
    ((max_len+=2))

    for var_name in "${vars_to_display[@]}"; do
        local value=$(get_env_variable "$var_name")
        local display_name="$var_name"
        local display_value=""
        local status_color="${RED}"
        local final_string=""

        if ! grep -q "^${var_name}=" "$ENV_FILE" 2>/dev/null; then
            display_value="Not Set"
            status_color="${RED}"
            if [[ "$var_name" == "ERGO_DONATION_PERCENTAGE" ]]; then
                display_name="Donation Percentage"
            fi
            final_string=$(printf "   %-*s : %s%s%s" $max_len "$display_name" "$status_color" "$display_value" "$NC")

        elif [[ "$var_name" == "ERGO_DONATION_PERCENTAGE" ]]; then
            display_name="Donation Percentage"
            if [ -z "$value" ]; then
                display_value="(empty)"
                status_color="${YELLOW}"
            else
                if (( $(echo "$value == 0" | bc -l) )); then
                    display_value="0%"
                    status_color="${YELLOW}"
                else
                    display_value=$(echo "scale=2; $value * 100 / 1" | bc | tr -d '\n')"%"
                    status_color="${GREEN}"
                fi
            fi
            final_string=$(printf "   %-*s : %s%s%s" $max_len "$display_name" "$status_color" "$display_value" "$NC")

        else
            if [ -z "$value" ]; then
                display_value="(empty)"
                status_color="${YELLOW}"
            else
                status_color="${GREEN}"
                if [[ "$var_name" == "ERGO_WALLET_MNEMONIC" ]] || [[ "$var_name" == "NGROK_TUNNELS_KEY" ]]; then
                    display_value="${value:0:5}...${value: -5}"
                else
                    display_value="$value"
                fi
            fi
            final_string=$(printf "   %-*s : %s%s%s" $max_len "$display_name" "$status_color" "$display_value" "$NC")
        fi
        printf "%s\n" "$final_string"
    done

    printf "%b\n" "${BLUE}=============================================================${NC}"
    printf "\n"
}

clear
printf "%b\n" "${BLUE}#############################################################${NC}"
printf "%b\n" "${BLUE}#${NC}                  ${YELLOW}Nodo Configuration Utility${NC}                 ${BLUE}#${NC}"
printf "%b\n" "${BLUE}#############################################################${NC}"
printf "\n"

display_summary
printf "%b\n" "${CYAN}Starting interactive configuration...${NC}"

handle_variable "ERGO_NODE_URL" validate_url
handle_variable "ERGO_WALLET_MNEMONIC" validate_wallet_address
handle_variable "REPUTATION_PROOF_ID" validate_reputation_id
handle_variable "ERGO_PAYMENTS_RECIVER_WALLET" validate_wallet_address

handle_donation

printf "\n"
printf "%b\n" "${MAGENTA}---------------------------------${NC}"
printf "%b\n" "${BLUE}Configuration process completed.${NC}"
printf "\n"
printf "%b\n" "${CYAN}Final Configuration:${NC}"
display_summary

printf "%b\n" "${GREEN}Exiting script.${NC}"
