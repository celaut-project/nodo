from multiprocessing import Lock
from signal import signal
from typing import Any, Dict, Generator, List, Tuple
import uuid
import celaut_pb2 as celaut
from utils import Singleton


class DuplicateGrabber(metaclass = Singleton):

    def __init__(self):
        self.hashes: Dict[celaut.Any.Metadata.HashTag.Hash, str] = {}
        self.sessions: Dict[str: Tuple[signal, Any] ] = {}
        self.lock = Lock()

    def next(self,
        hashes: List[celaut.Any.Metadata.HashTag.Hash], 
        generator: Generator
    ) -> Any:

        if True in [hashes in self.hashes.keys()]:
            session: str = self.hashes[hashes[0]]
            if self.sessions[session][0]:
                return self.sessions[session][1]

        else:
            
            session = uuid()
            for hash in hashes:
                self.hashes[hash] = session
            self.sessions[session] = (signal(), None)

            result = next(generator)
            return result
