#!/bin/bash

# Ruta al archivo .env
ENV_FILE=".env"

# Verifica si el archivo .env existe
if [ ! -f "$ENV_FILE" ]; then
    echo "El archivo .env no se encontró."
    exit 1
fi

# Función para leer una variable del archivo .env
get_env_variable() {
    local var_name=$1
    local value=$(grep -oP "^${var_name}=\K.*" "$ENV_FILE")
    echo "$value"
}

# Función para actualizar una variable en el archivo .env, manejando caracteres especiales
update_env_variable() {
    local var_name=$1
    local new_value=$2
    # Escapar caracteres especiales que puedan causar problemas en sed (/, &, etc.)
    escaped_value=$(echo "$new_value" | sed 's/[&/\]/\\&/g')
    sed -i "s|^${var_name}=.*|${var_name}=${escaped_value}|" "$ENV_FILE"
}

# Función para validar URLs
validate_url() {
    if [[ $1 =~ ^https?://.* ]]; then
        return 0
    else
        return 1
    fi
}

# Función para validar el formato de la dirección del wallet (simple validación para longitud)
validate_wallet_address() {
    if [[ ${#1} -ge 30 ]]; then
        return 0
    else
        return 1
    fi
}

# Función para mostrar y preguntar por una variable
handle_variable() {
    local var_name=$1
    local validation_function=$2
    local current_value=$(get_env_variable "$var_name")

    if [ -z "$current_value" ]; then
        echo "$var_name no tiene ningún valor asignado."
    else
        echo "Valor actual de $var_name: $current_value"
    fi

    read -p "¿Desea modificar $var_name? (s/n): " modify
    if [[ "$modify" =~ ^[sS]$ ]]; then
        local new_value=""
        while true; do
            read -p "Introduce el nuevo valor para $var_name: " new_value
            if $validation_function "$new_value"; then
                update_env_variable "$var_name" "$new_value"
                echo "$var_name actualizado correctamente."
                break
            else
                echo "Valor inválido. Inténtelo nuevamente."
            fi
        done
    fi
}

# Gestión de las variables
handle_variable "ERGO_NODE_URL" validate_url
handle_variable "ERGO_WALLET_MNEMONIC" validate_wallet_address
handle_variable "ERGO_PAYMENTS_RECIVER_WALLET" validate_wallet_address

echo "Proceso completado."
