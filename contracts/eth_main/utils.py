import typing
from web3.middleware import geth_poa_middleware
from web3 import HTTPProvider, Web3
import asyncio, time

from src.utils.logger import LOGGER
from src.utils.utils import get_ledger_providers


async def log_loop(event_filter, poll_interval: int, event_name: str, opt, w3, contract):
    while True:
        for event in event_filter.get_new_entries():
            receipt = w3.eth.waitForTransactionReceipt(event['transactionHash'])
            result = getattr(contract.events, event_name)().processReceipt(receipt)
            opt(args=result[0]['args'])
        time.sleep(poll_interval)


def catch_event(contract_address, w3, contract, event_name, opt, init_delay: int = 1, poll_interval: int = 1):
    while True:
        block: int = w3.eth.get_block('latest')['number'] - init_delay
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                asyncio.gather(
                    log_loop(
                        event_filter=w3.eth.filter({
                            'fromBlock': block,
                            'address': contract_address
                        }),
                        poll_interval=poll_interval, event_name=event_name, opt=opt, w3=w3, contract=contract
                    )))
        except Exception as e:
            LOGGER(f'Exception on catch event:  {str(e)}')
        finally:
            # close loop to free up system resources
            loop.close()


def transact(
        w3, method, priv, nonce, value=0, gas=2000000, pub=None, timeout=None, poll_latency=None,
) -> str:
    pub = w3.eth.account.privateKeyToAccount(priv).address if not pub else pub  # Not verify the correctness, 
    #     pub param is only for skip that step.
    transaction = method.buildTransaction({'gasPrice': w3.eth.gasPrice})
    transaction.update({
        'from': pub,  # Only 'from' address, don't insert 'to' address
        'value': value,  # Add how many ethers you'll transfer during the deploy
        'gas': gas,  # Trying to make it dynamic ..
        'nonce': nonce,  # Get Nonce
        'chainId': w3.eth.chainId,
    })
    # Sign the transaction using your private key
    signed = w3.eth.account.signTransaction(transaction, priv)
    tx_hash = w3.eth.sendRawTransaction(signed.rawTransaction).hex()
    LOGGER(f'Pub -> {pub} ')
    LOGGER(f'Transaction hash: {tx_hash}')
    LOGGER('Waiting for transaction to be mined...')
    if timeout and poll_latency: w3.eth.wait_for_transaction_receipt(tx_hash, timeout, poll_latency)
    LOGGER(f'Transaction hash: {tx_hash} executed and minted \n')
    return tx_hash


def check_provider_availability(provider) -> bool:
    return True  # TODO check if the provider is avialable.  ¿ping?


def w3_generator_factory(ledger: str) -> typing.Generator:
    print(f"generator w3 factory {ledger}")
    while True:
        for provider in get_ledger_providers(ledger=ledger):
            if not check_provider_availability(provider=provider):
                continue
            w3 = Web3(HTTPProvider(provider))
            w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            print(f"yield {w3}")
            yield w3
