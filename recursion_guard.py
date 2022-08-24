import uuid

recursion_guard_tokens = {}

class RecursionGuard(object):
    def __init__(self, token):
        self.token = token if token else uuid.uuid4().hex

    def __enter__(self):
        return self.token

    def __exit__()