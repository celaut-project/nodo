from wsgiref.simple_server import sys_version
import celaut_pb2
from iobigdata import mem_manager
import pymongo

db = pymongo.MongoClient(
            "mongodb://localhost:27017/"
        )["mongo"]["serviceInstances"]


def could_ve_this_sysreq(sysreq: celaut_pb2.Configuration.Sysinfo) -> bool:
    return True

def get_sysinfo(token: str) -> celaut_pb2.Configuration.Sysinfo:
    info = celaut_pb2.Configuration.Sysinfo()
    info = toProto( db.find(token) )
    return info

def modify_sysreq(token: str, sys_req: celaut_pb2.Configuration.Sysinfo):
    if (sys_req.hasField('ram_space')):
        with mem_manager(len = sys_req.ram_space):
            db.find(token)['ram_space'] = sys_req.ram_space