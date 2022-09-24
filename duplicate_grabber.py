from typing import Any, Dict, Generator, List
import uuid
import celaut_pb2 as celaut
from utils import Singleton
from threading import Event, Lock
import logger as l

class Session:
    
    def __init__(self) -> None:
        self.event = Event()
        self.value = None

    def wait(self):
        self.event.wait()

    def set(self):
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

        # hash.type.decode('utf-8')+':'+hash.value.decode('utf-8')  
        #  UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa7 in position 0: invalid start byte
        hashes: List[str] = [ str(hash) for hash in hashes ]

        if True in [hashes in self.hashes.keys()]:
            session: str = self.hashes[hashes[0]]
            l.LOGGER('It is already downloading. waiting for it to end. '+session)
            self.sessions[session].wait()
            if self.sessions[session].value is None: raise Exception('Session not ended.')
            value =  self.sessions[session].value
            return value

        else:
            session = uuid()
            l.LOGGER('Start download '+session)
            for hash in hashes:
                self.hashes[hash] = session
            self.sessions[session] = Session()

            result = next(generator)
            self.sessions[session].value = result
            self.sessions[session].set()
            return result
