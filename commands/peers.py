import os
import time

from tabulate import tabulate

from src.utils.utils import peers_id_iterator

while True:
    # Borrar la consola
    os.system('clear')

    # Crear una lista para almacenar los datos de cada peer
    datos_peeres = []

    # Recorrer los peeres y obtener los datos relevantes
    for peer in peers_id_iterator():

        # Agregar los datos a la lista
        datos_peeres.append([peer])

    # Mostrar la tabla de datos
    headers = ['PAR']
    print(tabulate(datos_peeres, headers=headers, tablefmt="plain"))

    # Esperar dos segundos antes de refrescar
    time.sleep(2)
