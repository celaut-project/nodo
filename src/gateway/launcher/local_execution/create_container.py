import podman as docker_lib

from src.utils import logger as l
from src.utils.env import DOCKER_CLIENT


def create_container(id: str, entrypoint: list, use_other_ports=None) -> docker_lib.models.containers.Container:
    try:
        return DOCKER_CLIENT().containers.create(
            image=id + '.docker',  # https://github.com/moby/moby/issues/20972#issuecomment-193381422
            entrypoint=' '.join(entrypoint),
            ports=use_other_ports
        )
    except docker_lib.errors.ImageNotFound as e:
        l.LOGGER('CONTAINER IMAGE NOT FOUND')
        # TODO build(id) using agents model.
        raise e
    except Exception as e:
        l.LOGGER('DOCKER RUN ERROR -> ' + str(e))
        raise e
