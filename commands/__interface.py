import os
import time
from typing import Callable, List
from tabulate import tabulate


def command(f: Callable, headers: List[str], sleep_time: int = 2):
    while True:
        # Clear the console
        os.system('clear')

        # Mostrar la tabla de datos
        print(tabulate([e for e in f()], headers=headers, tablefmt="plain"))

        # Wait for two seconds before refreshing
        time.sleep(sleep_time)
