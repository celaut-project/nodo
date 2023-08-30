SORTER = '7f4349da783e5ab0d0ad2644c43bfdf1861511861b6bc8d34c8b05453422098a'
RANDOM = '6f332226caa2fd444d99e72856019687bcbee392ec497e27d162a2f52c5b4239'
REGRESION = 'b9121eb90b1f74543ded21f70d3a1d8f9f3e2e6fb624adf0087ab8b405d3fb92'
FRONTIER = 'a62d187facd7621ad8f71eaaed72891ffda5732cbd043baab016fd84f5ed2299'
WALL = ''
WALK = ''

LISIADO_UNDER = ''
LISIADO_OVER = ''

SHA3_256 = 'a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a'

MOJITO = '192.168.1.14'
WHISKY = '192.168.1.78'
RON = '192.168.1.84'
LOCALHOST = 'localhost'
GATEWAY = RON

from protos import celaut_pb2, gateway_pb2
from grpcbigbuffer.client import Dir


def to_gas_amount(gas_amount: int) -> gateway_pb2.GasAmount:
    return gateway_pb2.GasAmount(n=str(gas_amount))


def from_gas_amount(gas_amount: gateway_pb2.GasAmount) -> int:
    i: int = str(gas_amount.gas_amount)[::-1].find('.')
    return int(gas_amount.gas_amount * pow(10, i) * pow(10, gas_amount.exponent - i))


def generator(_hash: str, mem_limit: int = 50 * pow(10, 6)):
    try:
        yield gateway_pb2.Client(client_id='dev')

        yield gateway_pb2.Configuration(
            config=celaut_pb2.Configuration(),
            resources=gateway_pb2.CombinationResources(
                clause={
                    1: gateway_pb2.CombinationResources.Clause(
                        cost_weight=1,
                        min_sysreq=celaut_pb2.Sysresources(
                                mem_limit=mem_limit
                            )
                    )
                }
            )
        )

        yield celaut_pb2.Any.Metadata.HashTag.Hash(
                type=bytes.fromhex(SHA3_256),
                value=bytes.fromhex(_hash)
            )

        yield Dir(
            dir='__metadata__/' + _hash,
            _type=celaut_pb2.Any.Metadata
        )

        yield Dir(
            dir='__registry__/' + _hash,
            _type=celaut_pb2.Service
        )

    except Exception as e:
        print(e)


def service_extended(hash):
    for t in generator(_hash=hash): yield t
