import os

def write_default_to_file(global_vars):
    # Ruta del archivo .env dentro de MAIN_DIR
    env_file_path = os.path.join(global_vars['MAIN_DIR'], ".env")

    # Verificar si el archivo .env ya existe
    if not os.path.exists(env_file_path):
        # Lista de variables a excluir
        exclude_vars = {
            "GET_ENV", "COMPILER_SUPPORTED_ARCHITECTURES", "SUPPORTED_ARCHITECTURES",
            "SHAKE_256_ID", "SHA3_256_ID", "SHAKE_256", "SHA3_256", "HASH_FUNCTIONS",
            "DOCKER_CLIENT", "DEFAULT_SYSTEM_RESOURCES", "DOCKER_COMMAND",
            "STORAGE", "CACHE", "REGISTRY", "METADATA_REGISTRY", "BLOCKDIR",
            "DATABASE_FILE", "REPUTATION_DB"
        }

        # Filtramos las variables que no están en la lista de exclusión y que son constantes (todo en mayúsculas)
        constants = {k: v for k, v in global_vars.items() if k not in exclude_vars and k.isupper()}

        # Escribimos las variables en el archivo .env
        with open(env_file_path, "w") as f:
            for key, value in constants.items():
                # Convertimos los valores booleanos a "True" o "False"
                if isinstance(value, bool):
                    value = "True" if value else "False"
                f.write(f"{key}={value}\n")

        print(f"Default environment variables written to {env_file_path}")
    else:
        print(f"The .env file already exists at {env_file_path}")
