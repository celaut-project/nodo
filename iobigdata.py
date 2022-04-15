# I/O Big Data utils.
import gc
from multiprocessing.connection import wait
from time import sleep
from threading import Lock

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

    def __init__(self, 
            log = lambda message: print(message),
            ram_pool_method = None,
            gas: int = 0,
            modify_resources = None
        ) -> None:

        self.ram_pool = ram_pool_method
        #self.gas = gas  # TODO will be a polynomy.
        self.modify_resources = modify_resources  # {min_memory_limit, max_memory_limit} -> memory_limit_updated

        self.log = log
        self.ram_locked = 0
        self.get_ram_avaliable = lambda: self.ram_pool() - self.ram_locked
        self.amount_lock = Lock()
        
        self.wait = []
        self.wait_lock = Lock()

    # General methods.

    def set_log(self, log = lambda message: print(message)) -> None:
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
        except ValueError: return "%s %s" % (size_bytes, size_name[0])

    def __stats(self, message: str):
        with self.amount_lock:
            self.log('\n--------- '+message+' -------------')
            self.log('RAM POOL       -> '+ IOBigData.convert_size(self.ram_pool()))
            self.log('RAM LOCKED     -> '+ IOBigData.convert_size(self.ram_locked))
            self.log('RAM AVALIABLE  -> '+ IOBigData.convert_size(self.get_ram_avaliable()))
            self.log('RAM WAITING    -> '+ IOBigData.convert_size(sum(self.wait)))
            #self.log('GAS            -> '+ str(self.gas))  TODO
            self.log('-----------------------------------------\n')



    # Gas manager methods.
    def __update_resources(self, modify_formula):
        if self.modify_resources:
            """
            if self.gas < sum(self.wait) and sum(self.wait) - self.gas < self.gas \
                or self.gas >= sum(self.wait) and self.gas < self.ram_locked:

                print(self.gas < sum(self.wait))
                print(sum(self.wait) - self.gas < self.gas)
                print(self.gas >= sum(self.wait)) 

                self.ram_pool = lambda: self.modify_resources((
                        self.ram_pool() + sum(self.wait) - self.gas,  # min resources.
                        self.ram_pool() + sum(self.wait) - self.gas  # max resources.
                ))
                self.gas += self.gas - sum(self.wait)        
            """

            v = self.modify_resources(
                {
                    "min": modify_formula(min),  # min resources.
                    "max": modify_formula(sum)   # max resources.
                }
            )
            self.ram_pool = lambda: v

    def __push_wait_list(self, l: int):
        with self.wait_lock:
            self.wait.append(l)
        if sum(self.wait) > self.get_ram_avaliable():
            with self.amount_lock:
                self.__update_resources(
                    modify_formula = lambda m: self.ram_locked + m(self.wait) # + self.gas * (X factor). TODO
                )

    def __pop_wait_list(self, l: int):
        with self.wait_lock:
            self.wait.remove(l)


    # Manage resources methods.

    def lock(self, len):
        return self.RamLocker(len = len, iobd = self)

    def lock_ram(self, ram_amount: int, wait: bool = True):
        self.__stats('want lock ' + IOBigData.convert_size(ram_amount))
        self.__push_wait_list(l=ram_amount)
        while True:
            self.__stats('go to lock ' + IOBigData.convert_size(ram_amount))
            if wait:
                self.wait_to_prevent_kill(len = ram_amount)

            elif not self.prevent_kill(len = ram_amount):
                self.__pop_wait_list(l=ram_amount)
                raise Exception

            with self.amount_lock:
                if self.get_ram_avaliable() > ram_amount:
                    self.ram_locked += ram_amount
                else:
                    continue
            self.__pop_wait_list(l=ram_amount)
            break
        self.__stats('locked ' + IOBigData.convert_size(ram_amount))

    def unlock_ram(self, ram_amount: int):
        with self.amount_lock:
            if ram_amount < self.ram_locked:
                self.ram_locked -= ram_amount
            else:
                self.ram_locked = 0

            if len(self.wait) == 0:
                self.__update_resources(
                    modify_formula = lambda m: self.ram_locked # + self.gas * (X factor). TODO
                )
        self.__stats('unlocked ' + IOBigData.convert_size(ram_amount))

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
