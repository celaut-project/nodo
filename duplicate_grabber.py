import itertools
from typing import Any, Dict, Generator, List, Tuple
from uuid import uuid4
import celaut_pb2 as celaut
from utils import Singleton
from threading import Event, Lock
import logger as l

class Session:
    
    def __init__(self) -> None:
        self.event: Event = Event()
        self.value: List = []

    def wait(self):
        self.event.wait()

    def set(self):
        self.event.set()

class DuplicateGrabber(metaclass=Singleton):

    def __init__(self):
        self.hashes: Dict[str, str] = {}
        self.sessions: Dict[str: Session ] = {}
        self.lock = Lock()

    def next(self,
        hashes: List[celaut.Any.Metadata.HashTag.Hash],
        generator: Generator
    ) -> Tuple[Any, bool]:

        # hash.type.decode('utf-8')+':'+hash.value.decode('utf-8')  
        #  UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa7 in position 0: invalid start byte
        hashes: List[str] = [ str(hash) for hash in hashes ]
        wait: bool = False

        with self.lock:
            for hash in hashes:
                if hash in self.hashes.keys():
                    session = self.hashes[hash]
                    wait = True
                    break
            
            if not wait:
                session = uuid4().hex
                l.LOGGER('Start download '+session)
                for hash in hashes:
                    self.hashes[hash] = session
                self.sessions[session] = Session()

        if wait:
            l.LOGGER('It is already downloading. waiting for it to end. '+session)
            self.sessions[session].wait()
            return self.sessions[session], False

        else:
            self.sessions[session].value = next(generator)
            self.sessions[session].set()
            return self.sessions[session].value, True
