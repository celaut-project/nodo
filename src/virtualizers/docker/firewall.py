from typing import Optional, List, Tuple
from dataclasses import dataclass
from datetime import datetime
import subprocess
import re
from protos import celaut_pb2 as celaut
from src.utils.logger import LOGGER as logger
from src.virtualizers.firewall import TransportProtocol, resolve_slot_transport_protocols

@dataclass
class NetworkRule:
    """Data class for storing network rule information."""
    container_id: str
    source_ip: str
    destination_ip: str
    destination_port: Optional[int]
    protocol: TransportProtocol
    created_at: datetime
    rule_number: Optional[int] = None

from src.utils.runtime import DOCKER_COMMAND, DOCKER_ENV

def __get_container_ip(container_id: str) -> str:
    """
    Get the IP address of a Docker container.
    """
    try:
        result = subprocess.run(
            DOCKER_COMMAND + [
                "inspect",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                container_id
            ],
            env=DOCKER_ENV,
            capture_output=True,
            text=True,
            check=True
        )
        ip = result.stdout.strip()
        if not ip:
            raise RuntimeError(f"Invalid IP address found for container {container_id}: {ip}")
        return ip
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to get container IP: {e}")

def __execute_iptables(command: List[str]) -> Tuple[bool, str]:
    """
    Execute an iptables command with additional security checks.
    """
    for arg in command:
        if not re.match(r'^[a-zA-Z0-9_\-.:/@]+$', str(arg)):
            raise ValueError(f"Invalid iptables argument format: {arg}")

    try:
        command.extend(['-m', 'comment', '--comment', 'nodo;docker'])
        result = subprocess.run(['iptables'] + command, capture_output=True, text=True, check=True)
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr

def block_all(container_id: str) -> bool:
    """
    Block all outgoing traffic from a specific container, except DNS.
    """
    try:
        container_ip = __get_container_ip(container_id)

        # Now drop all other NEW connections
        for protocol in TransportProtocol:
            success, message = __execute_iptables([
                '-I', 'FORWARD',
                '-s', container_ip,
                '-p', protocol.value,
                '-m', 'conntrack',
                '--ctstate', 'NEW',
                '-j', 'DROP'
            ])
            if not success:
                logger(f"Failed to block {protocol.value} traffic: {message}")
                return False

        logger(f"Blocked all outgoing traffic for container {container_id}")
        return True
    except Exception as e:
        logger(f"Failed to block all traffic: {e}")
        return False

def allow_connection(container_id: str, ip: str, port: Optional[int] = None, protocol: TransportProtocol = TransportProtocol.TCP) -> bool:
    """
    Allow outgoing traffic from container to specific IP and optional port.
    """
    try:
        container_ip = __get_container_ip(container_id)
        
        command = [
            '-I', 'FORWARD',
            '-s', container_ip,
            '-d', ip,
            '-p', protocol.value
        ]
        
        if port is not None:
            command.extend(['--dport', str(port)])
            
        command.extend(['-j', 'ACCEPT'])
        
        success, message = __execute_iptables(command)
        
        if success:
            logger(f"Allowed {protocol.value} connection from {container_id} to {ip}" +
                (f":{port}" if port else ""))
        else:
            logger(f"Failed to allow connection from {container_id} to {ip}: {message}")
            
        return success
    except Exception as e:
        logger(f"Failed to allow connection: {e}")
        return False
    
def allow_connection_to_instance(container_id: str, instance: celaut.Instance) -> bool:
    """
    Allow outgoing traffic from container to all IPs of a domain.
    """
    try:
        slot_protocols = {}
        for slot in instance.api.slot:
            slot_protocols[slot.port] = resolve_slot_transport_protocols(
                slot,
                logger_fn=logger,
                context=f"[DOCKER][FW][{container_id}]",
            )

        results = []
        for slot in instance.uri_slot:
            i_slot = slot.internal_port

            if i_slot not in slot_protocols:
                logger(f"Internal slot {i_slot} was not defined on instance.api.slot. Continue.")
                continue

            protocol = slot_protocols[i_slot]
            if not protocol:
                logger(f"Internal slot {i_slot} has no host-supported transports. Continue.")
                continue

            for uri in slot.uri:
                ip, port = uri.ip, uri.port
                result = allow_connection(container_id, ip, port, protocol)
                if not result:
                    logger(f"Failed to allow connection to an instance, continues to another slot.")
                results.append(result)

        final = any(results)
        if not final:
            raise Exception(f"Any of the slots were able to connect.")  # TODO Doesn't have sense exceptions and return bools. Fix that code.
        return final
    except Exception as e:
        logger(f"Failed to allow connection to an instance: {e}")
        return False


def remove_rule(container_id: str, ip: str, port: Optional[int] = None, protocol: TransportProtocol = TransportProtocol.TCP) -> bool:
    """
    Remove a previously created rule for a specific IP and port.
    """
    try:
        container_ip = __get_container_ip(container_id)
        
        command = [
            '-D', 'FORWARD',
            '-s', container_ip,
            '-d', ip,
            '-p', protocol.value
        ]
        
        if port is not None:
            command.extend(['--dport', str(port)])
            
        command.extend(['-j', 'ACCEPT'])
        
        success, message = __execute_iptables(command)
        
        if success:
            logger(f"Removed {protocol.value} rule for {container_id} to {ip}" +
                (f":{port}" if port else ""))
        else:
            logger(f"Failed to remove rule: {message}")
            
        return success
    except Exception as e:
        logger(f"Failed to remove rule: {e}")
        return False

def list_rules(container_id: str) -> List[NetworkRule]:
    """
    List all iptables rules for a specific container.
    """
    try:
        container_ip = __get_container_ip(container_id)
        rules = []
        
        for protocol in TransportProtocol:
            success, output = __execute_iptables(['-L', 'FORWARD', '-n', '--line-numbers'])
            if not success:
                logger(f"Failed to list {protocol.value} rules: {output}")
                continue
                
            for line in output.splitlines():
                if container_ip in line and protocol.value in line.lower():
                    port_match = re.search(r'dpt:(\d+)', line)
                    dst_ip_match = re.search(r'dst:(\d+\.\d+\.\d+\.\d+)', line)
                    rule_num_match = re.search(r'^\s*(\d+)', line)
                    
                    if dst_ip_match:
                        rule = NetworkRule(
                            container_id=container_id,
                            source_ip=container_ip,
                            destination_ip=dst_ip_match.group(1),
                            destination_port=int(port_match.group(1)) if port_match else None,
                            protocol=protocol,
                            created_at=datetime.now(),
                            rule_number=int(rule_num_match.group(1)) if rule_num_match else None
                        )
                        rules.append(rule)
        
        return rules
    except Exception as e:
        logger(f"Failed to list rules: {e}")
        return []

def allow_all_egress(container_id: str) -> bool:
    """Allow ALL outbound traffic from a container (network tag '*')."""
    try:
        container_ip = __get_container_ip(container_id)
        success, message = __execute_iptables([
            '-I', 'FORWARD',
            '-s', container_ip,
            '-j', 'ACCEPT',
        ])
        if success:
            logger(f"Allowed ALL egress for container {container_id} [network tag '*']")
        else:
            logger(f"Failed to allow all egress for container {container_id}: {message}")
        return success
    except Exception as e:
        logger(f"Failed to allow all egress: {e}")
        return False
