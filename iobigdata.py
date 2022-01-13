# I/O Big Data utils.
import psutil, gc
from time import sleep
from threading import Lock

# Thread-sage Singleton
import threading
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

mem_manager = lambda len: IOBigData().lock(len=len)

def read_file(filename) -> bytes:
    def generator(filename):
        with open(filename, 'rb') as entry:
            for chunk in iter(lambda: entry.read(1024 * 1024), b''):
                    yield chunk
    return b''.join([b for b in generator(filename)])

class IOBigData(metaclass=Singleton):

    class RamLocker(object):
        def __init__(self, len, iobd):
            self.len = len
            self.iobd = iobd

        def __enter__(self):
            self.iobd.lock_ram(ram_amount = self.len)
            return self

        def unlock(self, amount: int):
            self.iobd.unlock_ram(ram_amount = amount)
            self.len -= amount

        def __exit__(self, type, value, traceback):
            self.iobd.unlock_ram(ram_amount = self.len)
            gc.collect()

    def __init__(self, log = lambda message: print(message)) -> None:
        self.log = log
        self.ram_pool = lambda: psutil.virtual_memory().available
        self.ram_locked = 0
        self.get_ram_avaliable = lambda: self.ram_pool() - self.ram_locked
        self.amount_lock = Lock()

    def set_log(self, log = lambda message: print(message)) -> None:
        self.log = log

    def stats(self, message: str):
        with self.amount_lock:
            self.log('\n--------- '+message+' -------------')
            self.log('RAM POOL      -> '+ str(self.ram_pool()))
            self.log('RAM LOCKED    -> '+ str(self.ram_locked))
            self.log('RAM AVALIABLE -> '+ str(self.get_ram_avaliable()))
            self.log('-----------------------------------------\n')

    def lock(self, len):
        return self.RamLocker(len = len, iobd = self)

    def lock_ram(self, ram_amount: int, wait: bool = True):
        self.stats('go to lock ' + str(ram_amount))
        if wait:
            self.wait_to_prevent_kill(len = ram_amount)
        elif not self.prevent_kill(len = ram_amount):
            raise Exception
        with self.amount_lock:
            self.ram_locked += ram_amount
        self.stats('locked')

    def unlock_ram(self, ram_amount: int):
        with self.amount_lock:
            if ram_amount < self.ram_locked:
                self.ram_locked -= ram_amount
            else:
                self.ram_locked = 0
        self.stats('unlocked')

    def prevent_kill(self, len: int) -> bool:
        with self.amount_lock:
            b = self.get_ram_avaliable() > len
        return b

    def wait_to_prevent_kill(self, len: int) -> None:
        while True:
            if not self.prevent_kill(len = len):
                sleep(1)
            else:
                return