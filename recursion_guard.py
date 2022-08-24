import uuid, threading

class Singleton(type):
  _instances = {}
  _lock = threading.Lock()

  def __call__(cls):
    if cls not in cls._instances:
      with cls._lock:
        # another thread could have created the instance
        # before we acquired the lock. So check that the
        # instance is still nonexistent.
        if cls not in cls._instances:
          cls._instances[cls] = super().__call__()
    return cls._instances[cls]

class Registry(Singleton):

    def __init__(self):
        self.tokens = {}

    def add(self, token):
        self.tokens[token] = None

    def delete(self, token):
        del self.tokens[token]

class RecursionGuard(object):

    def __init__(self, token):
        self.token = token if token else uuid.uuid4().hex

        if self.token in Registry().tokens:
            raise Exception('Raise recursion guard.')
        else:
            Registry().add(self.token)

    def __enter__(self):
        return self.token

    def __exit__(self, exception_type, exception_value, traceback):
        Registry().delete(self.token)