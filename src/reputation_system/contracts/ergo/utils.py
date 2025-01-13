from binascii import hexlify
from ergpy import appkit as ergpy
from ergpy.helper_functions import initialize_jvm

from jpype import *
import java.lang

from org.ergoplatform.sdk import *
from org.ergoplatform.appkit import *
from org.ergoplatform.appkit.impl import *
from org.ergoplatform import *
# import sigmastate.basics.DLogProtocol.ProveDlog
# import sigma.GroupElement

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

"""
@initialize_jvm
def pub_key_hex_to_addr(pub_key_hex: str) -> str:
    
    publicKeyBytes = bytes.fromhex(pub_key_hex)
    
    publicKey = GroupElement.fromBytes(publicKeyBytes);
    
    proveDlog = ProveDlog.apply(publicKey);
    
    address = Address.fromErgoTree(proveDlog.ergoTree(), NetworkType.MAINNET);
    
    return address
"""

@initialize_jvm
def addr_to_pub_key_hex(address: str) -> str:
    
    def ecp_to_hex(point):
        # Get x and y coordinates as hex strings without '0x' prefix
        x_hex = format(point.x(), 'x').zfill(64)
        y_hex = format(point.y(), 'x').zfill(64)
        # Concatenate with '02' or '03' prefix based on y being even/odd
        prefix = '02' if point.y % 2 == 0 else '03'
        return prefix + x_hex
        
    pk = address.getPublicKey()
    return ecp_to_hex(pk.value())