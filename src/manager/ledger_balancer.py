import typing


def ledger_balander(ledgers: typing.Generator[typing.Tuple[str, str], None, None]) \
        -> typing.Generator[typing.Tuple[str, str], None, None]:
    # TODO Implementar un ledger balancer para decidir que instancia del
    #  contrato utilizar. Ademas de filtrar entre los soportados por si mismo (por este nodo).
    for ledger_generator in ledgers:
        yield ledger_generator
