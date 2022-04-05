from wsgiref.simple_server import sys_version
import celaut_pb2
from iobigdata import mem_manager
import pymongo

db = pymongo.MongoClient(
            "mongodb://localhost:27017/"
        )["mongo"]["serviceInstances"]


# I've it's not possible local, but other pair can, returns True.
def could_ve_this_sysreq(sysreq: celaut_pb2.Configuration.Sysparams) -> bool:
    return True

def get_sysparams(token: str) -> celaut_pb2.Configuration.Sysparams:
    info = celaut_pb2.Configuration.Sysparams()
    info = toProto( db.find(token) )
    return info

def modify_sysreq(token: str, sys_req: celaut_pb2.Configuration.Sysparams):
    if (sys_req.hasField('ram_space')):
        with mem_manager(len = sys_req.ram_space):
            db.find(token)['ram_space'] = sys_req.ram_space