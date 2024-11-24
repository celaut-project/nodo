from enum import Enum
from typing import Optional, List, Tuple
from dataclasses import dataclass
from datetime import datetime
import subprocess
import re
import ipaddress
from src.utils.logger import LOGGER as log

class Protocol(Enum):
    """Supported network protocols."""
    TCP = "tcp"
    UDP = "udp"

@dataclass
class NetworkRule:
    """Data class for storing network rule information."""
    container_id: str
    source_ip: str
    destination_ip: str
    destination_port: Optional[int]
    protocol: Protocol
    created_at: datetime
    rule_number: Optional[int] = None

def validate_container_id(container_id: str) -> bool:
    """
    Validate if the provided container ID exists and is running.
    """
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]+$', container_id):
        raise ValueError("Invalid container ID format - potential security risk")
        
    try:
        result = subprocess.run(
            ['docker', 'inspect', '--format', '{{.State.Running}}', container_id],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip() == 'true'
    except subprocess.CalledProcessError:
        return False

def validate_ip(ip: str) -> bool:
    """
    Validate if the provided IP address is valid and not in reserved ranges.
    """
    try:
        addr = ipaddress.ip_address(ip)
        if addr.is_loopback or addr.is_link_local or addr.is_multicast:
            log(f"IP {ip} is in a reserved range")
            return False
        return True
    except ValueError:
        return False

def validate_port(port: Optional[int]) -> bool:
    """
    Validate if the provided port number is valid and not in restricted range.
    """
    if port is None:
        return True
        
    if not isinstance(port, int):
        return False
        
    if port < 1024:
        log(f"Port {port} is in privileged range")
    elif port > 65535:
        return False
        
    return 1 <= port <= 65535

def get_container_ip(container_id: str) -> str:
    """
    Get the IP address of a Docker container.
    """
    try:
        result = subprocess.run(
            ['docker', 'inspect', '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', container_id],
            capture_output=True,
            text=True,
            check=True
        )
        ip = result.stdout.strip()
        if not ip or not validate_ip(ip):
            raise RuntimeError(f"Invalid IP address found for container {container_id}: {ip}")
        return ip
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to get container IP: {e}")

def execute_iptables(command: List[str], check_exists: bool = False) -> Tuple[bool, str]:
    """
    Execute an iptables command with additional security checks.
    """
    for arg in command:
        if not re.match(r'^[a-zA-Z0-9_\-.:/@]+$', str(arg)):
            raise ValueError(f"Invalid iptables argument format: {arg}")

    try:
        if check_exists:
            check_command = ['iptables', '-C'] + command[1:]
            try:
                subprocess.run(check_command, capture_output=True, check=True)
                return False, "Rule already exists"
            except subprocess.CalledProcessError:
                pass

        result = subprocess.run(['iptables'] + command, capture_output=True, text=True, check=True)
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr

def block_all(container_id: str) -> bool:
    """
    Block all outgoing traffic from a specific container.
    """
    if not validate_container_id(container_id):
        raise ValueError(f"Invalid or non-running container: {container_id}")

    container_ip = get_container_ip(container_id)
    
    for protocol in Protocol:
        success, message = execute_iptables([
            '-I', 'FORWARD',
            '-s', container_ip,
            '-p', protocol.value,
            '-j', 'DROP'
        ])
        if not success:
            log(f"Failed to block {protocol.value} traffic: {message}")
            return False

    log(f"Blocked all outgoing traffic for container {container_id}")
    return True

def allow_connection(container_id: str, ip: str, port: Optional[int] = None, protocol: Protocol = Protocol.TCP) -> bool:
    """
    Allow outgoing traffic from container to specific IP and optional port.
    """
    if not validate_container_id(container_id):
        raise ValueError(f"Invalid or non-running container: {container_id}")
    if not validate_ip(ip):
        raise ValueError(f"Invalid IP address: {ip}")
    if not validate_port(port):
        raise ValueError(f"Invalid port number: {port}")

    container_ip = get_container_ip(container_id)
    
    command = [
        '-I', 'FORWARD',
        '-s', container_ip,
        '-d', ip,
        '-p', protocol.value
    ]
    
    if port is not None:
        command.extend(['--dport', str(port)])
        
    command.extend(['-j', 'ACCEPT'])
    
    success, message = execute_iptables(command, check_exists=True)
    
    if success:
        log(f"Allowed {protocol.value} connection from {container_id} to {ip}" +
            (f":{port}" if port else ""))
    else:
        log(f"Failed to allow connection: {message}")
        
    return success

def remove_rule(container_id: str, ip: str, port: Optional[int] = None, protocol: Protocol = Protocol.TCP) -> bool:
    """
    Remove a previously created rule for a specific IP and port.
    """
    if not validate_container_id(container_id):
        raise ValueError(f"Invalid or non-running container: {container_id}")
    if not validate_ip(ip):
        raise ValueError(f"Invalid IP address: {ip}")
    if not validate_port(port):
        raise ValueError(f"Invalid port number: {port}")

    container_ip = get_container_ip(container_id)
    
    command = [
        '-D', 'FORWARD',
        '-s', container_ip,
        '-d', ip,
        '-p', protocol.value
    ]
    
    if port is not None:
        command.extend(['--dport', str(port)])
        
    command.extend(['-j', 'ACCEPT'])
    
    success, message = execute_iptables(command)
    
    if success:
        log(f"Removed {protocol.value} rule for {container_id} to {ip}" +
            (f":{port}" if port else ""))
    else:
        log(f"Failed to remove rule: {message}")
        
    return success

def list_rules(container_id: str) -> List[NetworkRule]:
    """
    List all iptables rules for a specific container.
    """
    if not validate_container_id(container_id):
        raise ValueError(f"Invalid or non-running container: {container_id}")

    container_ip = get_container_ip(container_id)
    rules = []
    
    for protocol in Protocol:
        success, output = execute_iptables(['-L', 'FORWARD', '-n', '--line-numbers'])
        if not success:
            log(f"Failed to list {protocol.value} rules: {output}")
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
