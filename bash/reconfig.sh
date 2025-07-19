#!/bin/bash

CONFIG_FILE="config.yaml"

# --- Prerrequisitos: Comprobar si yq y bc están instalados ---
if ! command -v yq &> /dev/null; then
    echo -e "\033[1;31mError: 'yq' no está instalado o no se encuentra en el PATH.\033[0m"
    echo -e "\033[0;33m'yq' es necesario para leer y escribir de forma segura en el archivo de configuración YAML.\033[0m"
    echo -e "\033[0;32mPara más información, visita: https://github.com/mikefarah/yq/\033[0m"
    exit 1
fi

if ! command -v bc &> /dev/null; then
    echo -e "\033[1;31mError: 'bc' no está instalado o no se encuentra en el PATH.\033[0m"
    echo -e "\033[0;33m'bc' es necesario para las validaciones de números decimales.\033[0m"
    exit 1
fi


# --- Comprobar si el archivo de configuración existe ---
if [ ! -f "$CONFIG_FILE" ]; then
    printf "\033[1;31mError: El archivo de configuración '%s' no se encontró en el directorio actual.\033[0m\n" "$CONFIG_FILE"
    exit 1
fi

# --- Definiciones de Color ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[1;34m'
MAGENTA='\033[1;35m'
CYAN='\033[0;36m'
NC='\033[0m' # Sin Color

# --- Lista Maestra de Todas las Variables Configurables ---
# Mantener esta lista actualizada es clave para el contador de progreso.
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

# --- Funciones de Utilidad (yq) ---
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
        return 1 # Falso (no está configurada)
    else
        return 0 # Verdadero (está configurada)
    fi
}

# --- Funciones de Validación (sin cambios) ---
validate_url() { if [[ $1 =~ ^https?://.* ]]; then return 0; else printf "%b\n" "${RED}   -> URL inválida. Debe empezar con http:// o https://${NC}"; return 1; fi; }
validate_wallet_address() { if [[ ${#1} -ge 30 ]]; then return 0; else printf "%b\n" "${RED}   -> Formato inválido. Se esperan al menos 30 caracteres.${NC}"; return 1; fi; }
validate_reputation_id() { if [[ ${#1} -ge 30 ]]; then return 0; else printf "%b\n" "${RED}   -> Formato inválido. Se esperan al menos 30 caracteres.${NC}"; return 1; fi; }
validate_percentage() { if [[ $1 =~ ^[0-9]+(\.[0-9]+)?$ ]] && (( $(echo "$1 >= 0 && $1 <= 100" | bc -l) )); then return 0; else printf "%b\n" "${RED}   -> Porcentaje inválido. Introduce un número entre 0 y 100.${NC}"; return 1; fi; }
validate_integer() { if [[ $1 =~ ^-?[0-9]+$ ]]; then return 0; else printf "%b\n" "${RED}   -> Entrada inválida. Introduce un número entero.${NC}"; return 1; fi; }
validate_boolean() { if [[ "$1" == "true" || "$1" == "false" ]]; then return 0; else printf "%b\n" "${RED}   -> Entrada inválida. Introduce 'true' o 'false'.${NC}"; return 1; fi; }

# --- Manejador de Entrada Interactivo (sin cambios) ---
handle_variable() {
    local key=$1
    local description=$2
    local validation_function=$3
    local current_value=$(get_yaml_variable "$key")
    printf "%b\n" "${MAGENTA}-----------------------------------------------------${NC}"
    printf "%b\n" "${CYAN}Configurando: ${YELLOW}${description}${NC}"
    if ! is_variable_set "$key"; then
        printf "   Valor actual: ${YELLOW}(no establecido)${NC}\n"
    elif [[ "$key" == "ledgers.ergo.WALLET_MNEMONIC" || "$key" == "network.NGROK_TUNNELS_KEY" ]]; then
        printf "   Valor actual: ${GREEN}${current_value:0:5}...${current_value: -5}${NC}\n"
    else
        printf "   Valor actual: ${GREEN}${current_value}${NC}\n"
    fi
    local new_value
    while true; do
        printf "%b" "${YELLOW}   -> Introduce un nuevo valor o pulsa [Enter] para mantener el actual: ${NC}"
        read -r new_value
        if [ -z "$new_value" ]; then
            printf "%b\n" "${CYAN}   No se han realizado cambios.${NC}"
            break
        fi
        if [ -n "$validation_function" ]; then
            if $validation_function "$new_value"; then
                update_yaml_variable "$key" "$new_value"
                printf "%b\n" "${GREEN}   => Valor actualizado correctamente.${NC}"
                break
            else
                printf "%b\n" "${RED}   => Entrada inválida. Por favor, inténtalo de nuevo.${NC}"
            fi
        else
            update_yaml_variable "$key" "$new_value"
            printf "%b\n" "${GREEN}   => Valor actualizado correctamente.${NC}"
            break
        fi
    done; printf "\n"
}

# --- Funciones para Cada Categoría de Configuración ---
configure_ledgers() {
    handle_variable "ledgers.ergo.NODE_URL" "URL del Nodo Ergo" validate_url
    handle_variable "ledgers.ergo.WALLET_MNEMONIC" "Mnemónico de la Wallet Ergo" validate_wallet_address
}
configure_reputation() {
    handle_variable "reputation.REPUTATION_PROOF_ID" "ID de Prueba de Reputación" validate_reputation_id
}
configure_payments() {
    handle_variable "payments.PAYMENTS_RECEIVER_WALLET" "Wallet Receptora de Pagos" validate_wallet_address
    handle_variable "payments.DONATION_PERCENTAGE" "Porcentaje de Donación (ej: 5.5)" validate_percentage
}
configure_network() {
    handle_variable "network.NGROK_TUNNELS_KEY" "Clave de Túneles NGROK"
}
configure_costs() {
    handle_variable "costs.FREE_GAS_THRESHOLD" "Umbral de Gas Gratuito" validate_integer
    handle_variable "costs.SOCIALIZATION_FACTOR" "Factor de Socialización" validate_integer
    handle_variable "costs.ALLOW_GAS_DEBT" "Permitir Deuda de Gas (true/false)" validate_boolean
}
configure_packer() {
    handle_variable "packer.SAVE_ALL" "Packer: Guardar todos los items (true/false)" validate_boolean
}
configure_communication() {
    handle_variable "communication.SEND_INSTANCE" "Comunicación: Enviar Instancia"
    handle_variable "communication.SEND_ONLY_HASHES_ASKING_COST" "Comunicación: Enviar solo hashes al pedir coste"
    handle_variable "communication.DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH" "Comunicación: Denegar petición de coste si no se tiene el hash"
}
configure_misc() {
    handle_variable "misc.VALIDATE_ON_IMPORT" "Misc: Validar al importar (true/false)" validate_boolean
}
configure_logs() {
    handle_variable "logs.DEBUG_MODE" "Modo Debug (true/false)" validate_boolean
}
configure_all_unset() {
    printf "\n${CYAN}Configurando todas las variables pendientes...${NC}\n"
    for var in "${ALL_VARIABLES[@]}"; do
        if ! is_variable_set "$var"; then
            # Extraer descripción y validador (requiere una lógica más compleja o una definición explícita)
            # Por simplicidad, llamaremos a la función de categoría correspondiente si alguna está incompleta
            # O, más simple aún, se llama a handle_variable directamente con datos predefinidos
            # Para este script, vamos a mantenerlo simple y solo informar. El usuario puede seleccionar la categoría.
            # Una implementación más avanzada mapearía variables a sus funciones de handle_variable.
            # ---
            # La implementación más directa es simplemente llamar a handle_variable para la variable pendiente
            # con sus parámetros predefinidos. Esto implica algo de duplicación de código.
            # Vamos a simplificarlo: llamamos a la función de la categoría si encontramos una variable sin configurar.
            
            # Buscamos el nombre de la categoría para una mejor descripción
            local category_name=$(echo "$var" | cut -d. -f1 | sed 's/.*/\u&/')
            local description=$(echo "$var" | sed 's/\./ /g' | sed 's/_/ /g' | awk '{for(i=1;i<=NF;i++) $i=toupper(substr($i,1,1)) substr($i,2); print $0}')
            
            # Esto se vuelve muy complejo de mapear sin una estructura de datos más avanzada (ej. arrays asociativos en Bash 4+)
            # Para mantener la compatibilidad y simplicidad, esta función simplemente listará lo que falta.
            # El usuario puede entonces elegir la categoría.
            # Una mejora sería llamar a la función de `handle_variable` para cada una.
            # Vamos a implementar el flujo completo para una mejor UX.
            # Esta sección se ha vuelto más compleja, así que la omitimos en favor de que el usuario elija la categoría.
            echo "La variable '$var' está pendiente."
        fi
    done
    
    # Un enfoque más práctico para "configurar todo"
    for var in "${ALL_VARIABLES[@]}"; do
        if ! is_variable_set "$var"; then
             # Esto requiere un mapeo de variable -> descripción y validador, lo que es complejo en Bash.
             # La forma más limpia es que el usuario elija la sección incompleta desde el menú.
             # Por lo tanto, esta función solo mostrará un resumen.
             : # Placeholder
        fi
    done
    printf "\n${YELLOW}Por favor, selecciona una de las categorías marcadas como incompletas para continuar.${NC}\n"
    sleep 3
}

# --- Lógica del Menú Principal ---
while true; do
    clear
    printf "%b\n" "${BLUE}#############################################################${NC}"
    printf "%b\n" "${BLUE}#${NC}        ${YELLOW}Utilidad de Configuración del Nodo${NC}           ${BLUE}#${NC}"
    printf "%b\n" "${BLUE}#############################################################${NC}"

    # --- Calcular y mostrar el estado de la configuración ---
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
    printf "\n${status_color}Estado de la Configuración: ${set_count} de ${TOTAL_VARS} variables configuradas.${NC}\n\n"
    
    # Función para obtener el estado de una categoría
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
            echo -e "${GREEN}($count/$total configuradas)${NC}"
        else
            echo -e "${YELLOW}($count/$total configuradas)${NC}"
        fi
    }
    
    # Opciones del Menú
    cat_ledgers_vars=("ledgers.ergo.NODE_URL" "ledgers.ergo.WALLET_MNEMONIC")
    cat_reputation_vars=("reputation.REPUTATION_PROOF_ID")
    cat_payments_vars=("payments.PAYMENTS_RECEIVER_WALLET" "payments.DONATION_PERCENTAGE")
    cat_network_vars=("network.NGROK_TUNNELS_KEY")
    cat_costs_vars=("costs.FREE_GAS_THRESHOLD" "costs.SOCIALIZATION_FACTOR" "costs.ALLOW_GAS_DEBT")
    cat_packer_vars=("packer.SAVE_ALL")
    cat_comm_vars=("communication.SEND_INSTANCE" "communication.SEND_ONLY_HASHES_ASKING_COST" "communication.DENEGATE_COST_REQUEST_IF_DONT_VE_THE_HASH")
    cat_misc_vars=("misc.VALIDATE_ON_IMPORT")
    cat_logs_vars=("logs.DEBUG_MODE")

    printf "Selecciona una categoría para configurar:\n"
    printf " 1) Ledgers        %s\n" "$(get_category_status "${cat_ledgers_vars[@]}")"
    printf " 2) Reputación     %s\n" "$(get_category_status "${cat_reputation_vars[@]}")"
    printf " 3) Pagos          %s\n" "$(get_category_status "${cat_payments_vars[@]}")"
    printf " 4) Red            %s\n" "$(get_category_status "${cat_network_vars[@]}")"
    printf " 5) Costos         %s\n" "$(get_category_status "${cat_costs_vars[@]}")"
    printf " 6) Packer         %s\n" "$(get_category_status "${cat_packer_vars[@]}")"
    printf " 7) Comunicación   %s\n" "$(get_category_status "${cat_comm_vars[@]}")"
    printf " 8) Misc           %s\n" "$(get_category_status "${cat_misc_vars[@]}")"
    printf " 9) Logs           %s\n" "$(get_category_status "${cat_logs_vars[@]}")"
    printf -- "-----------------------------------------------------\n"
    if [ "$set_count" -ne "$TOTAL_VARS" ]; then
        printf "${CYAN}10) Configurar TODAS las variables pendientes...${NC}\n"
    fi
    printf " 0) Salir\n\n"

    printf "${YELLOW}Elige una opción: ${NC}"
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
            # Lógica para configurar todo lo pendiente.
            # Llama a cada función de configuración una por una.
            # Es más sencillo que un bucle complejo.
            printf "\n${CYAN}Iniciando configuración de todas las variables pendientes...${NC}\n\n"
            configure_ledgers
            configure_reputation
            configure_payments
            configure_network
            configure_costs
            configure_packer
            configure_communication
            configure_misc
            configure_logs
            printf "${GREEN}Revisión completa de todas las categorías.${NC}\n"
            sleep 2
            ;;
        0) break ;;
        *) printf "\n${RED}Opción no válida. Inténtalo de nuevo.${NC}\n"; sleep 1 ;;
    esac
done

# --- Finalización ---
printf "\n"
printf "%b\n" "${MAGENTA}-----------------------------------------------------${NC}"
printf "%b\n" "${BLUE}Proceso de configuración finalizado.${NC}"
printf "\n"
printf "%b\n" "${GREEN}El archivo '$CONFIG_FILE' ha sido actualizado.${NC}"
printf "\n"

exit 0