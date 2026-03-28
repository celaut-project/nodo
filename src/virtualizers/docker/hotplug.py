from bee_rpc import client as bee
from protos import celaut_pb2, celaut_pb2
from src.utils import logger as log
from src.utils.runtime import DOCKER_CLIENT
from src.manager.modify_resources import modify_sysreq
import docker as docker_lib
from src.utils.config import ConfigManager

env_manager = ConfigManager()
MEMSWAP_FACTOR = env_manager.get("MEMSWAP_FACTOR")

def __get_container_by_id(id: str) -> docker_lib.models.containers.Container:
    return DOCKER_CLIENT().containers.get(
        container_id=id
    )


def hotplug(
        container_id: str,
        system_requeriments_range: celaut_pb2.ModifyServiceSystemResourcesInput
) -> bool:
    _id = container_id[:6]
    log.LOGGER(f'Modify params of {_id}')

    # https://docker-py.readthedocs.io/en/stable/containers.html#docker.models.containers.Container.update
    # Set system requeriments parameters.

    system_requeriments = system_requeriments_range.max_sysreq # TODO: Implement the use of min_sysreq in case there are not enough max_sysreq, it can be lower as long as it does not go below the minimum threshold
    if not system_requeriments: 
        log.LOGGER(f"No system requeriments for {_id}")
        return False

    # Docker has a minimum of 6Mb of mem limit.
    if system_requeriments.mem_limit < 6*10**6:
        system_requeriments.mem_limit = 6*10**6

    if modify_sysreq(
            id=container_id,
            sys_req=system_requeriments
    ):
        try:
            # Memory limit should be smaller than already set memoryswap limit, update the memoryswap at the same time
            container = __get_container_by_id(id=container_id)
            container.update(
                mem_limit=system_requeriments.mem_limit if MEMSWAP_FACTOR == 0 \
                    else system_requeriments.mem_limit - MEMSWAP_FACTOR * system_requeriments.mem_limit,
                memswap_limit=system_requeriments.mem_limit if MEMSWAP_FACTOR > 0 else -1
            )
        except Exception as e:
            log.LOGGER(f"Docker container for {_id} fail with e: {str(e)}")
            # TODO reset modified system req.  Maybe the __get_container_by_id should be inside of __modify_sysreq.
            return False
        
        log.LOGGER(f"System params modified correctly for {_id}.")
        return True

    log.LOGGER(f"System req could not be modified for {container_id}: mem limit {system_requeriments.mem_limit}")
    return False