import re
import subprocess
from typing import List, Optional, Tuple

from protos import celaut_pb2 as celaut
from src.database.sql_connection import SQLConnection
from src.utils.logger import LOGGER as logger
from src.virtualizers.ch.runtime_state import load_runtime_state
from src.virtualizers.firewall import TransportProtocol, resolve_slot_transport_protocols

sc = SQLConnection()


def _execute_iptables(command: List[str]) -> Tuple[bool, str]:
    for index, arg in enumerate(command):
        arg_text = str(arg)
        is_comment_value = index > 0 and command[index - 1] == "--comment"
        pattern = r"^[a-zA-Z0-9_\-.:/@;=]+$" if is_comment_value else r"^[a-zA-Z0-9_\-.:/@]+$"
        if not re.match(pattern, arg_text):
            raise ValueError(f"Invalid iptables argument format: {arg_text}")

    # Ensure contains comment
    if "-m" not in command or "comment" not in command:
        raise ValueError("iptables command must include a comment for auditing purposes.")

    try:
        result = subprocess.run(
            ["iptables"] + command,
            capture_output=True,
            text=True,
            check=True,
        )
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr


def _resolve_vmachine_ip(vmachine_id: str, source_ip: Optional[str] = None) -> str:
    if source_ip and str(source_ip).strip():
        return str(source_ip).strip()

    state = load_runtime_state(vmachine_id)
    if state:
        ip = state.get("ip")
        if isinstance(ip, str) and ip.strip():
            return ip.strip()

    ip = sc.get_internal_ip(id=vmachine_id)
    if ip and str(ip).strip():
        return str(ip).strip()

    raise RuntimeError(f"Unable to resolve VM IP for firewall rules: {vmachine_id}")


def block_all(vmachine_id: str, source_ip: Optional[str] = None) -> bool:
    try:
        vm_ip = _resolve_vmachine_ip(vmachine_id=vmachine_id, source_ip=source_ip)

        for protocol in TransportProtocol:
            success, message = _execute_iptables(
                [
                    "-I",
                    "FORWARD",
                    "-s",
                    vm_ip,
                    "-p",
                    protocol.value,
                    "-m",
                    "conntrack",
                    "--ctstate",
                    "NEW",
                    "-j",
                    "DROP",
                    "-m", "comment",
                    "--comment", f"nodo;block_all;vm={vmachine_id}",
                ]
            )
            if not success:
                logger(
                    f"[CH][FW] Failed to block {protocol.value} traffic for {vmachine_id} ({vm_ip}): {message}"
                )
                return False

        logger(f"[CH][FW] Blocked all outgoing traffic for {vmachine_id} ({vm_ip})")
        return True
    except Exception as e:
        logger(f"[CH][FW] Failed to block all traffic for {vmachine_id}: {e}")
        return False


def allow_connection(
    vmachine_id: str,
    ip: str,
    port: Optional[int] = None,
    protocol: TransportProtocol = TransportProtocol.TCP,
    source_ip: Optional[str] = None,
) -> bool:
    try:
        vm_ip = _resolve_vmachine_ip(vmachine_id=vmachine_id, source_ip=source_ip)
        command = [
            "-I",
            "FORWARD",
            "-s",
            vm_ip,
            "-d",
            ip,
            "-p",
            protocol.value,
        ]
        if port is not None:
            command.extend(["--dport", str(port)])
        command.extend(["-j", "ACCEPT"])

        command.extend(['-m', 'comment', '--comment', f"nodo;allow;vm={vmachine_id}"])

        success, message = _execute_iptables(command)
        if success:
            logger(
                f"[CH][FW] Allowed {protocol.value} from {vmachine_id} ({vm_ip}) to {ip}"
                + (f":{port}" if port is not None else "")
            )
        else:
            logger(
                f"[CH][FW] Failed to allow {protocol.value} from {vmachine_id} ({vm_ip}) "
                f"to {ip}{f':{port}' if port is not None else ''}: {message}"
            )
        return success
    except Exception as e:
        logger(f"[CH][FW] Failed to allow connection for {vmachine_id}: {e}")
        return False


def allow_connection_to_instance(
    vmachine_id: str,
    instance: celaut.Instance,
    source_ip: Optional[str] = None,
) -> bool:
    try:
        slot_protocols = {
            slot.port: resolve_slot_transport_protocols(
                slot,
                logger_fn=logger,
                context=f"[CH][FW][{vmachine_id}]",
            )
            for slot in instance.api.slot
        }

        results: List[bool] = []
        for slot in instance.uri_slot:
            internal_port = slot.internal_port
            if internal_port not in slot_protocols:
                logger(
                    f"[CH][FW] Internal slot {internal_port} not present in instance.api.slot. Skipping."
                )
                continue
            protocol = slot_protocols[internal_port]
            if not protocol:
                logger(
                    f"[CH][FW] Internal slot {internal_port} has no host-supported transports. Skipping."
                )
                continue

            for uri in slot.uri:
                result = allow_connection(
                    vmachine_id=vmachine_id,
                    ip=uri.ip,
                    port=uri.port,
                    protocol=protocol,
                    source_ip=source_ip,
                )
                if not result:
                    logger(
                        f"[CH][FW] Failed allow_connection_to_instance for {vmachine_id} "
                        f"towards {uri.ip}:{uri.port}/{protocol.value}"
                    )
                results.append(result)

        if not any(results):
            raise RuntimeError("No allow rule could be applied for any instance slot.")
        return True
    except Exception as e:
        logger(f"[CH][FW] Failed to allow connection to instance for {vmachine_id}: {e}")
        return False


def remove_rule(
    vmachine_id: str,
    ip: str,
    port: Optional[int] = None,
    protocol: TransportProtocol = TransportProtocol.TCP,
    source_ip: Optional[str] = None,
) -> bool:
    try:
        vm_ip = _resolve_vmachine_ip(vmachine_id=vmachine_id, source_ip=source_ip)
        command = [
            "-D",
            "FORWARD",
            "-s",
            vm_ip,
            "-d",
            ip,
            "-p",
            protocol.value,
        ]
        if port is not None:
            command.extend(["--dport", str(port)])
        command.extend(["-j", "ACCEPT"])

        command.extend(['-m', 'comment', '--comment', f"nodo;remove;vm={vmachine_id}"])

        success, message = _execute_iptables(command)
        if success:
            logger(
                f"[CH][FW] Removed {protocol.value} rule for {vmachine_id} ({vm_ip}) to {ip}"
                + (f":{port}" if port is not None else "")
            )
        else:
            logger(
                f"[CH][FW] Failed to remove rule for {vmachine_id} ({vm_ip}) to {ip}"
                + (f":{port}" if port is not None else "")
                + f": {message}"
            )
        return success
    except Exception as e:
        logger(f"[CH][FW] Failed to remove rule for {vmachine_id}: {e}")
        return False

def allow_all_egress(vmachine_id: str, source_ip: Optional[str] = None) -> bool:
    """Allow ALL outbound traffic from the VM (used for network tag '*').

    Inserted at the head of FORWARD so it short-circuits the block_all DROP rule.
    Return traffic is covered by the global ESTABLISHED/RELATED accept rule.
    """
    try:
        vm_ip = _resolve_vmachine_ip(vmachine_id=vmachine_id, source_ip=source_ip)
        success, message = _execute_iptables([
            "-I", "FORWARD",
            "-s", vm_ip,
            "-m", "comment",
            "--comment", f"nodo;allow_all_egress;vm={vmachine_id}",
            "-j", "ACCEPT",
        ])
        if success:
            logger(f"[CH][FW] Allowed ALL egress for {vmachine_id} ({vm_ip}) [network tag '*']")
        else:
            logger(f"[CH][FW] Failed to allow all egress for {vmachine_id} ({vm_ip}): {message}")
        return success
    except Exception as e:
        logger(f"[CH][FW] Failed to allow all egress for {vmachine_id}: {e}")
        return False
