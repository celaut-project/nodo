from protos import celaut_pb2
from src.manager.resources import IOBigData, could_ve_this_extra_memory
from src.database.sql_connection import SQLConnection
from src.utils import host_limits
from src.utils import logger as log

def _growth(new, current):
    """How much more of a resource a resize wants, or None when it wants no more.

    None rather than 0, because `host_limits.ceiling_shortfalls` treats the two
    differently: a figure is compared against the ceiling, and None means "do not ask
    about this resource at all".
    """
    delta = new - current
    return delta if delta > 0 else None


def modify_sysreq(id: str, sys_req: celaut_pb2.Sysresources) -> bool:
    sc = SQLConnection()

    if not sc.internal_instance_exists(id=id):
        log.LOGGER(f'Manager error: container {id} does not exists.')
        return False
    
    current_sys_req = sc.get_sys_req(id=id)
    current_mem_limit = current_sys_req['mem_limit']
    current_disk_space = current_sys_req['disk_space']
    current_cpu_period = current_sys_req['cpu_period']
    current_cpu_quota = current_sys_req['cpu_quota']

    if sys_req.HasField('mem_limit'):
        variation = sys_req.mem_limit - current_mem_limit
        log.LOGGER(f"Modify memory with variation of {variation}: {current_mem_limit} -> {sys_req.mem_limit}")
        IOBigData().log_snapshot(
            context=f"modify-sysreq:before id={id} current={current_mem_limit} target={sys_req.mem_limit}"
        )

        # Only growth has to be found in the pool. The instance already holds
        # `current_mem_limit`, so a resize that shrinks it -- or that re-states the
        # limit it already has -- can never fail for lack of memory.
        #
        # This used to ask `could_ve_this_sysreq` about the absolute `mem_limit`, as
        # if the whole figure were a fresh allocation on top of what the instance
        # was already given. With the host's pool low that rejected requests asking
        # for nothing: a service re-affirming its 64 MiB ceiling (which is how
        # node_controller's modify_resources reads back a balance) got
        # "Insufficient memory." and the caller got `Exception on service modify
        # method.`, and so did a request to *release* memory -- the one operation
        # that makes memory available.
        if not could_ve_this_extra_memory(extra_mem_bytes=variation):
            log.LOGGER(
                f"Insufficient memory: growing {id} by "
                f"{IOBigData.convert_size(variation)} does not fit in the service pool."
            )
            return False
        
        # if variation > 0:
        #     IOBigData().lock_ram(ram_amount=abs(variation))

        # elif variation < 0:
        #     IOBigData().unlock_ram(ram_amount=abs(variation))
        IOBigData().log_snapshot(
            context=f"modify-sysreq:after id={id} current={current_mem_limit} target={sys_req.mem_limit}"
        )

    new_mem_limit = sys_req.mem_limit if sys_req.HasField('mem_limit') else current_mem_limit
    new_disk_space = sys_req.disk_space if sys_req.HasField('disk_space') else current_disk_space
    # Persist the CFS pair too, so a CPU resize is recorded rather than enforced on
    # the guest while the row keeps its stale value and the caller is told it
    # persisted (#249).
    new_cpu_period = sys_req.cpu_period if sys_req.HasField('cpu_period') else current_cpu_period
    new_cpu_quota = sys_req.cpu_quota if sys_req.HasField('cpu_quota') else current_cpu_quota

    # The operator's own ceiling (`host_limits`), against the *growth* only, and only
    # for the resources that actually grow. What the instance already holds is part of
    # the committed sum this is compared with, so passing the absolute figures would
    # count the same bytes twice and refuse a resize that asks for nothing. Passing None
    # for a resource that is shrinking or unchanged is what keeps a node already over
    # its ceiling -- the shares were lowered underneath it -- from refusing the very
    # resize that would bring it back under.
    growth_shortfalls = host_limits.ceiling_shortfalls(
        cores=_growth(
            host_limits.requested_cores(new_cpu_quota or 0, new_cpu_period or 0),
            host_limits.requested_cores(current_cpu_quota or 0, current_cpu_period or 0),
        ),
        ram_bytes=_growth(new_mem_limit or 0, current_mem_limit or 0),
        disk_bytes=_growth(new_disk_space or 0, current_disk_space or 0),
    )
    if growth_shortfalls:
        for shortfall in growth_shortfalls:
            log.LOGGER(f"Refusing to resize {id}: {shortfall}")
        return False

    if (new_mem_limit != current_mem_limit or new_disk_space != current_disk_space
            or new_cpu_period != current_cpu_period or new_cpu_quota != current_cpu_quota):
        if not sc.update_sys_req(
            id=id,
            mem_limit=new_mem_limit,
            disk_space=new_disk_space,
            cpu_period=new_cpu_period,
            cpu_quota=new_cpu_quota,
        ):
            return False

    return True
