from protos import celaut_pb2
from src.manager.resources import IOBigData, could_ve_this_sysreq
from src.database.sql_connection import SQLConnection
from src.utils import logger as log

def modify_sysreq(id: str, sys_req: celaut_pb2.Sysresources) -> bool:
    sc = SQLConnection()

    if not sc.internal_instance_exists(id=id):
        log.LOGGER(f'Manager error: container {id} does not exists.')
        return False
    
    if sys_req.HasField('mem_limit'):
        current_mem_limit = sc.get_sys_req(id=id)['mem_limit']
        variation = sys_req.mem_limit - current_mem_limit
        log.LOGGER(f"Modify memory with variation of {variation}: {current_mem_limit} -> {sys_req.mem_limit}")
        IOBigData().log_snapshot(
            context=f"modify-sysreq:before id={id} current={current_mem_limit} target={sys_req.mem_limit}"
        )

        if not could_ve_this_sysreq(sysreq=sys_req):
            log.LOGGER("Insufficient memory.")
            return False
        
        if variation > 0:
            IOBigData().lock_ram(ram_amount=abs(variation))

        elif variation < 0:
            IOBigData().unlock_ram(ram_amount=abs(variation))

        if variation != 0:
            sc.update_sys_req(id=id, mem_limit=sys_req.mem_limit)
        IOBigData().log_snapshot(
            context=f"modify-sysreq:after id={id} current={current_mem_limit} target={sys_req.mem_limit}"
        )

    return True
