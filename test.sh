# Accept KyA
TARGET_DIR = "/nodo"
ACCEPT_KYA_SCRIPT="bash/accept_kya.sh"
chmod +x "$ACCEPT_KYA_SCRIPT"
if ! ./"$ACCEPT_KYA_SCRIPT" "$TARGET_DIR"; then
  printf "Error: The script $ACCEPT_KYA_SCRIPT failed to execute.\n" >&2
  exit 1
fi

printf "Installation and service setup completed successfully. The repository is located at $TARGET_DIR.\n"
printf "********** You can now use the 'nodo' command. **********\n"

# Run Nodo configuration script.
# Accept KyA
CONFIG_SCRIPT="bash/reconfig.sh"
chmod +x "$CONFIG_SCRIPT"
if ! ./"$CONFIG_SCRIPT"; then
  printf "Error: The script $CONFIG_SCRIPT failed to execute.\n" >&2
  exit 1
fi
