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