# I/O Big Data utils.
import gc
import os
import re
import time
from time import sleep
from threading import Lock, RLock
import threading
from typing import Optional
import psutil
from protos import celaut_pb2

class Singleton(type):
    _instances = {}
    _lock = threading.Lock()

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with cls._lock:
                # another thread could have created the instance
                # before we acquired the lock. So check that the
                # instance is still nonexistent.
                if cls not in cls._instances:
                    cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


mem_manager = lambda len, timeout=None: IOBigData().lock(len=len, timeout=timeout)

PREVENT_KILL_WAIT_TIME = 5 # seconds

class IOBigData(metaclass=Singleton):
    class RamLocker(object):
        def __init__(self, len, iobd, timeout=None):
            self.len = len
            self.iobd = iobd
            self.timeout = timeout

        def __enter__(self):
            self.iobd.lock_ram(ram_amount=self.len, timeout=self.timeout)
            return self

        def unlock(self, amount: int):
            self.iobd.unlock_ram(ram_amount=amount)
            self.len -= amount

        def __exit__(self, type, value, traceback):
            self.iobd.unlock_ram(ram_amount=self.len)
            gc.collect()

    def __init__(self,
                 log=lambda message: print(message),
                 ram_pool_method=None
                 ) -> None:

        self._initial_python_rss_bytes = _python_rss_bytes()  # Consumo del intérprete de python al iniciar el proceso, tomado como referencia de uso base para evitar el doble conteo con ram_locked. Se considera que esto es lo que gasta fuera del locked.

        def default_ram_pool():
            sys_available = psutil.virtual_memory().available
            nodo_rss, nodo_reserved = _get_nodo_ch_memory_stats()
            
            # Solo restamos el crecimiento potencial de las VMs
            potential_vm_growth = max(0, nodo_reserved - nodo_rss) 
            
            # Compensamos el consumo actual del daemon para evitar el doble conteo con ram_locked
            daemon_growth = min(self.ram_locked, _python_rss_bytes() - self._initial_python_rss_bytes)  #  El min se usa para evitar que se hubiera restado ram_locked pero aún no se reflejara en el RSS del proceso, lo que podría llevar a un conteo negativo (mas disponible del que realmente hay).
            
            return sys_available - potential_vm_growth + daemon_growth

        self.ram_pool = ram_pool_method if ram_pool_method is not None else default_ram_pool

        self.log = log
        self.ram_locked = 0  
        self.get_ram_avaliable = lambda: self.ram_pool() - self.ram_locked
        self.amount_lock = RLock()

        self.waiting_bytes = 0
        self.wait_lock = Lock()

    # General methods.

    def set_log(self, log=lambda message: print(message)) -> None:
        self.log = log

    @staticmethod
    def convert_size(size_bytes):
        import math
        if size_bytes == 0:
            return "0B"
        size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
        try:
            i = int(math.floor(math.log(size_bytes, 1024)))
            p = math.pow(1024, i)
            s = round(size_bytes / p, 2)
            return "%s %s" % (s, size_name[i])
        except ValueError:
            return "%s %s" % (size_bytes, size_name[0])

    def snapshot(self) -> dict:
        system_available = int(psutil.virtual_memory().available)
        with self.amount_lock:
            ram_locked = int(self.ram_locked)
            pool_available = int(self.ram_pool())
            effective_available = int(self.get_ram_avaliable())
            with self.wait_lock:
                waiting = int(self.waiting_bytes)
                
        return {
            "pid": os.getpid(),
            "system_available": system_available,
            "pool_available": pool_available,
            "ram_locked": ram_locked,
            "effective_available": effective_available,
            "waiting": waiting,
        }

    def log_snapshot(self, context: str) -> None:
        snapshot = self.snapshot()
        self.log(
            "[MEM] "
            f"{context} | pid={snapshot['pid']} | "
            f"system_available={IOBigData.convert_size(snapshot['system_available'])} | "
            f"pool_available={IOBigData.convert_size(snapshot['pool_available'])} | "
            f"ram_locked={IOBigData.convert_size(snapshot['ram_locked'])} | "
            f"effective_available={IOBigData.convert_size(snapshot['effective_available'])} | "
            f"waiting={IOBigData.convert_size(snapshot['waiting'])}"
        )

    def __stats(self, message: str, comments: bool = True):
        if comments:
            with self.amount_lock:
                nodo_rss, nodo_reserved = _get_nodo_ch_memory_stats()
                current_python_rss = _python_rss_bytes()
                
                self.log('\n--------- ' + message + ' -------------')
                self.log('SYSTEM AVAILABLE -> ' + IOBigData.convert_size(psutil.virtual_memory().available))
                self.log('VMACHINES RSS     -> ' + IOBigData.convert_size(nodo_rss))
                self.log('VMACHINES RESERVED -> ' + IOBigData.convert_size(nodo_reserved))
                self.log('VMACHINES NOT USED   -> ' + IOBigData.convert_size(max(0, nodo_reserved - nodo_rss)))
                self.log('DAEMON RSS      -> ' + IOBigData.convert_size(current_python_rss))
                self.log('DAEMON RSS INI  -> ' + IOBigData.convert_size(self._initial_python_rss_bytes))
                self.log('DAEMON RSS ON LOCK -> ' + IOBigData.convert_size(max(0, current_python_rss - self._initial_python_rss_bytes)))
                self.log('RAM POOL        -> ' + IOBigData.convert_size(self.ram_pool()))
                self.log('RAM LOCKED      -> ' + IOBigData.convert_size(self.ram_locked))
                self.log('RAM AVAILABLE   -> ' + IOBigData.convert_size(self.get_ram_avaliable()))
                with self.wait_lock:
                    self.log('RAM WAITING     -> ' + IOBigData.convert_size(self.waiting_bytes))
                self.log('-----------------------------------------\n')

    # Gas manager methods.
    def __push_wait_list(self, l: int):
        with self.wait_lock:
            self.waiting_bytes += l

    def __pop_wait_list(self, l: int):
        with self.wait_lock:
            self.waiting_bytes -= l
            if self.waiting_bytes < 0:
                self.waiting_bytes = 0

    def __can_lock_ram(self, ram_amount: int, *, inclusive: bool) -> bool:
        with self.amount_lock:
            available = self.get_ram_avaliable()
            return available >= ram_amount if inclusive else available > ram_amount

    # Manage resources methods.

    def lock(self, len, timeout=None):
        return self.RamLocker(len=len, iobd=self, timeout=timeout)

    # Lock_ram y unlock_Ram son usados en __enter__ y __exit__ de RamLocker.
    def lock_ram(self, ram_amount: int, wait: bool = True, timeout: Optional[float] = None):
        self.__stats('want lock ' + IOBigData.convert_size(ram_amount))
        self.__push_wait_list(l=ram_amount)
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                self.__stats('go to lock ' + IOBigData.convert_size(ram_amount))
                if wait:
                    self.wait_to_prevent_kill(len=ram_amount, deadline=deadline)

                elif not self.__can_lock_ram(ram_amount=ram_amount, inclusive=True):
                    raise Exception

                with self.amount_lock:
                    if self.__can_lock_ram(ram_amount=ram_amount, inclusive=True):
                        self.ram_locked += ram_amount
                    else:
                        continue
                break
        except Exception:
            self.__pop_wait_list(l=ram_amount)
            raise
        self.__pop_wait_list(l=ram_amount)
        self.__stats('locked ' + IOBigData.convert_size(ram_amount))

    def unlock_ram(self, ram_amount: int):
        with self.amount_lock:
            if ram_amount < self.ram_locked:
                self.ram_locked -= ram_amount
            else:
                self.ram_locked = 0

        self.__stats('unlocked ' + IOBigData.convert_size(ram_amount))

    def prevent_kill(self, len: int) -> bool:
        b = self.__can_lock_ram(ram_amount=len, inclusive=False)
        self.__stats('[prevent kill] Try to take ' + IOBigData.convert_size(len) + '. Takes it:' + str(b))
        return b

    def wait_to_prevent_kill(self, len: int, deadline: Optional[float] = None) -> None:
        while True:
            if not self.__can_lock_ram(ram_amount=len, inclusive=True):
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out waiting to unlock memory for {IOBigData.convert_size(len)}"
                    )
                sleep(PREVENT_KILL_WAIT_TIME)
            else:
                return

def could_ve_this_sysreq(sysreq: celaut_pb2.Sysresources) -> bool:
    return IOBigData().prevent_kill(len=sysreq.mem_limit)  

def _get_nodo_ch_memory_stats() -> tuple[int, int]:
    """Devuelve una tupla con (total_rss_bytes, total_reserved_bytes) en una sola pasada."""
    total_rss = 0
    total_reserved = 0
    for p in psutil.process_iter(["name", "cmdline", "memory_info"]):
        try:
            cmdline = " ".join(p.info.get("cmdline") or [])
            if "nodo-ch" in cmdline:
                mem_info = p.info.get("memory_info")
                if mem_info:
                    total_rss += mem_info.rss
                total_reserved += __parse_memory_from_cmdline(cmdline)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, TypeError):
            continue
    return total_rss, total_reserved

def _python_rss_bytes():
    return psutil.Process(os.getpid()).memory_info().rss

def __parse_memory_from_cmdline(cmdline: str) -> int:
    """Parsea la memoria reservada del cmdline de nodo-ch."""
    match = re.search(r'--memory\s+size=(\d+)([KMGT]?)', cmdline, re.IGNORECASE)
    if not match:
        return 0
    
    value = int(match.group(1))
    unit = match.group(2).upper() if match.group(2) else 'M'  
    
    multipliers = {
        'K': 1024,
        'M': 1024 ** 2,
        'G': 1024 ** 3,
        'T': 1024 ** 4,
    }
    
    return value * multipliers.get(unit, 1024 ** 2)