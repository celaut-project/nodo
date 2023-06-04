import os
import time
from tabulate import tabulate

from src.utils.utils import get_ledgers


def contracts():
    while True:
        # Borrar la consola
        os.system('clear')

        # Crear una lista para almacenar los datos de cada peer
        datos_peeres = []

        # Recorrer los peeres y obtener los datos relevantes
        for ledger_id, private_key in get_ledgers():

            # Agregar los datos a la lista
            datos_peeres.append([ledger_id, private_key])

        # Mostrar la tabla de datos
        headers = ['LEDGER', 'PRIVATE KEY']
        print(tabulate(datos_peeres, headers=headers, tablefmt="plain"))

        # Esperar dos segundos antes de refrescar
        time.sleep(2)
