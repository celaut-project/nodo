import typing


def ledger_balancer(ledger_generator: typing.Generator[typing.Tuple[str, str], None, None]) \
        -> typing.Generator[typing.Tuple[str, str], None, None]:
    # TODO Implementar un ledger balancer para decidir que instancia del
    #  contrato utilizar. Ademas de filtrar entre los soportados por si mismo (por este nodo).
    for element in ledger_generator:
        yield element
