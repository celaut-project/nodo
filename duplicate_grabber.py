from multiprocessing import Lock
from typing import Any, Dict, Generator, List
import uuid
import celaut_pb2 as celaut
from utils import Singleton
from threading import Barrier, Event, Lock

class Session:
    
    def __init__(self) -> None:
        self.event = Event()
        self.value = None
        self.barrier_count_lock = Lock()
        self.barrier_count = 0

    def wait(self):
        with self.barrier_count_lock:
            self.barrier_count += 1
        self.event.wait()

    def set(self):
        self.barrier = Barrier(self.barrier_count)
        self.event.set()

class DuplicateGrabber(metaclass=Singleton):

    def __init__(self):
        self.hashes: Dict[celaut.Any.Metadata.HashTag.Hash, str] = {}
        self.sessions: Dict[str: Session ] = {}
        self.lock = Lock()

    def next(self,
        hashes: List[celaut.Any.Metadata.HashTag.Hash],
        generator: Generator
    ) -> Any:

        if True in [hashes in self.hashes.keys()]:
            session: str = self.hashes[hashes[0]]
            self.sessions[session].wait()
            if self.sessions[session].value is None: raise Exception('Session not ended.')
            value =  self.sessions[session].value
            self.sessions[session].barrier.wait()
            return value

        else:
            session = uuid()
            for hash in hashes:
                self.hashes[hash] = session
            self.sessions[session] = Session()

            result = next(generator)
            self.sessions[session].value = result
            self.sessions[session].set()
            return result
