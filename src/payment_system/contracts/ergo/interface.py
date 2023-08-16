from ergpy import ergo

# URL del nodo Ergo
node_url = "https://your-node-url.com"
ergo_client = ergo.ErgoClient(node_url)

# Frase mnemónica de la billetera
mnemonic = "your mnemonic phrase here"
wallet = ergo.Wallet(mnemonic)

# Dirección del contrato
contract_address = "your_contract_address_here"

# Cargar el contrato
contract = ergo.Contract(ergo_client, contract_address)


# Obtener el propietario del contrato
def get_contract_owner():
    owner = contract.call("get_owner")
    return owner


# Agregar gas al contrato
def add_gas_to_contract(token_id, amount):
    tx = wallet.send_to_contract(
        contract_address,
        "add_gas",
        [{"token": token_id, "amount": amount}]
    )
    return tx


# Obtener información sobre el contrato
def get_contract_info():
    parity_factor = contract.call("get_parity_factor")
    return {"parity_factor": parity_factor}


# Interactuar con el contrato
def interact_with_contract():
    # Obtener el propietario del contrato
    owner = get_contract_owner()
    print("Contract Owner:", owner)

    # Agregar gas al contrato
    token_id = b'\x01\x02\x03'
