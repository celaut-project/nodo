import uuid
from src.utils.singleton import Singleton


class Registry(metaclass=Singleton):

    def __init__(self):
        self.tokens = {}

    def add(self, token):
        self.tokens[token] = None

    def delete(self, token):
        if token:
            del self.tokens[token]


class RecursionGuard(object):

    def __init__(self, token: str, generate: bool):
        if generate:
            self.token = token if token else uuid.uuid4().hex

            if self.token in Registry().tokens:
                raise Exception('Block recursion loop.')
            else:
                Registry().add(self.token)

        else:
            self.token = None

    def __enter__(self):
        return self.token

    def __exit__(self, exception_type, exception_value, traceback):
        Registry().delete(self.token)
