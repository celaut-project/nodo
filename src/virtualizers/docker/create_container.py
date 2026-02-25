import json
import os
from typing import List

import docker as docker_lib

from src.utils import logger as log
from src.utils.config import DOCKER_CLIENT, ConfigManager

env_manager = ConfigManager()
_missing_seccomp_profile_warned = False


def _is_apparmor_enabled() -> bool:
    try:
        with open("/sys/module/apparmor/parameters/enabled", "r", encoding="utf-8") as f:
            return f.read().strip().lower().startswith("y")
    except OSError:
        return False


def _is_selinux_enabled() -> bool:
    try:
        with open("/sys/fs/selinux/enforce", "r", encoding="utf-8") as f:
            return f.read().strip() in {"0", "1"}
    except OSError:
        return False


def _default_seccomp_profile_path() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "seccomp.json"))


def get_container_security_opts() -> List[str]:
    global _missing_seccomp_profile_warned

    security_opts: List[str] = []
    seccomp_profile_path = _default_seccomp_profile_path()

    if seccomp_profile_path:
        if os.path.isfile(seccomp_profile_path):
            with open(seccomp_profile_path, "r", encoding="utf-8") as f:
                seccomp_content = json.dumps(json.load(f), separators=(",", ":"))
            security_opts.append(f"seccomp={seccomp_content}")
        elif not _missing_seccomp_profile_warned:
            log.LOGGER(
                f"Missing seccomp profile at {seccomp_profile_path}. "
                f"Using Docker default seccomp profile."
            )
            _missing_seccomp_profile_warned = True

    if env_manager.get("docker.SECURITY_APPARMOR_UNCONFINED", True) and _is_apparmor_enabled():
        security_opts.append("apparmor=unconfined")

    if env_manager.get("docker.SECURITY_SELINUX_DISABLE_LABEL", True) and _is_selinux_enabled():
        security_opts.append("label=disable")

    return security_opts


def create_container(id: str, entrypoint: list, use_other_ports=None) -> docker_lib.models.containers.Container:
    try:
        create_args = {
            "image": id + '.docker',  # https://github.com/moby/moby/issues/20972#issuecomment-193381422
            "entrypoint": ' '.join(entrypoint),
            "ports": use_other_ports,
            "dns": ["127.0.0.1"]
        }

        security_opts = get_container_security_opts()
        if security_opts:
            create_args["security_opt"] = security_opts

        return DOCKER_CLIENT().containers.create(
            **create_args
        )
    except docker_lib.errors.ImageNotFound as e:
        log.LOGGER('CONTAINER IMAGE NOT FOUND')
        # TODO build(id) using agents model.
        raise e
    except Exception as e:
        log.LOGGER('DOCKER RUN ERROR -> ' + str(e))
        raise e
