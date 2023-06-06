import os
import time
from typing import Callable, List
from tabulate import tabulate


def table_command(f: Callable, headers: List[str], stream: bool = True, sleep_time: int = 2):
    while stream:
        # Clear the console
        if stream:
            os.system('clear')

        # Mostrar la tabla de datos
        print(tabulate([e for e in f()], headers=headers, tablefmt="plain"))

        if not stream:
            exit()

        # Wait for two seconds before refreshing
        time.sleep(sleep_time)
