# Import external packages
from ergpy import appkit
import jpype

# Initialize JVM before calling the function
def initialize_jvm(function):
    def wrapper(*args, **kwargs):
        try:
            jpype.addClassPath('ergo.jar')
            jpype.startJVM()
        except OSError:
            pass
        finally:
            res = function(*args, **kwargs)
            return res
    return wrapper

@initialize_jvm
def create_reputation_proof_tx(ergo: appkit.ErgoAppKit, wallet_mnemonic: str, object_to_assign: dict = None,
                               input_proof: dict = None, mnemonic_password: str = None, prover_index: int = 0,
                               return_signed=False, fee=1000000):
    # 1. Obtener la dirección de cambio
    mnemonic = ergo.getMnemonic(wallet_mnemonic=wallet_mnemonic, mnemonic_password=mnemonic_password)
    sender_address = ergo.getSenderAddress(index=0, wallet_mnemonic=mnemonic[1], wallet_password=mnemonic[2])

    # 2. Preparar las entradas de la transacción (obtener UTXOs)
    # Obtener UTXOs disponibles para cubrir la transacción
    input_boxes = ergo.getInputBoxCovering(amount_list=[fee], sender_address=sender_address)

    if input_proof:
        # Añadir caja adicional si hay una prueba previa
        input_boxes += [input_proof['box']]

    # 3. Construir las salidas de la transacción
    outputs = []

    if input_proof is None:
        # Si no hay prueba previa, minar un nuevo token
        token_id = ergo.mintToken(sender_address, "ReputationToken", "Token de Reputación", 1)
        out_box = ergo.buildOutBox(value=1000000, address=sender_address, tokens={token_id: 1})
    else:
        # Si existe una prueba previa, añadir los tokens a la nueva caja
        tokens = input_proof['tokens']
        out_box = ergo.buildOutBox(value=1000000, address=sender_address, tokens=tokens)

    # Añadir el out_box a las salidas
    outputs.append(out_box)

    # 4. Configurar los registros adicionales
    if object_to_assign:
        out_box.additionalRegisters[4] = ergo.encode_string(object_to_assign['data1'])
        out_box.additionalRegisters[5] = ergo.encode_string(object_to_assign['data2'])

    # 5. Construir y firmar la transacción
    unsigned_tx = ergo.buildUnsignedTransaction(inputs=input_boxes, outputs=outputs, fee=fee, changeAddress=sender_address)
    signed_tx = ergo.signTransaction(unsigned_tx, mnemonic[0], prover_index)

    # 6. Enviar la transacción y devolver el ID
    tx_id = ergo.submitTransaction(signed_tx)

    if return_signed:
        return signed_tx

    return tx_id
