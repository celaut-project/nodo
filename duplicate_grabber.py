from multiprocessing import Lock
from typing import Any, Dict, Generator, List, Tuple
import uuid
import celaut_pb2 as celaut
from utils import Singleton
from threading import Event


class DuplicateGrabber(metaclass=Singleton):

    def __init__(self):
        self.hashes: Dict[celaut.Any.Metadata.HashTag.Hash, str] = {}
        self.sessions: Dict[str: Tuple[Event, Any] ] = {}
        self.lock = Lock()

    def next(self,
        hashes: List[celaut.Any.Metadata.HashTag.Hash],
        generator: Generator
    ) -> Any:

        if True in [hashes in self.hashes.keys()]:
            session: str = self.hashes[hashes[0]]
            self.sessions[session][0].wait()
            if self.sessions[session][1] is None: raise Exception('Session not ended.')
            return self.sessions[session][1]

        else:
            session = uuid()
            for hash in hashes:
                self.hashes[hash] = session
            self.sessions[session] = (Event(), None)

            result = next(generator)
            self.sessions[session][1] = result
            self.sessions[session][0].set()
            return result
