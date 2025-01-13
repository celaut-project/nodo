from ergpy import appkit as ergpy
from ergpy.helper_functions import initialize_jvm

from jpype import *
import java.lang

from org.ergoplatform.sdk import *
from org.ergoplatform.appkit import *
from org.ergoplatform.appkit.impl import *
from org.ergoplatform import *

from src.utils.env import EnvManager

def get_public_key(mnemonic_phrase: str) -> str:
    """
    Obtains the public key in hexadecimal format from the mnemonic phrase.

    :param mnemonic_phrase: BIP-39 mnemonic phrase.
    :return: Public key in hexadecimal format.
    """
    ergo = ergpy.ErgoAppKit(node_url=EnvManager().get_env("ERGO_NODE_URL"))
    mnemonic = ergo.getMnemonic(wallet_mnemonic=mnemonic_phrase, mnemonic_password=None)
    return ergo.getSenderAddress(index=0, wallet_mnemonic=mnemonic[1], wallet_password=mnemonic[2])

@initialize_jvm
def pub_key_hex_to_addr(pub_key_hex: str) -> str:
    
    address = Address.create(pub_key_hex)
    
    return address
