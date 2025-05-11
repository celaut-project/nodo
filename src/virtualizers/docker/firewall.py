from enum import Enum
import socket
from typing import Optional, List, Tuple
from dataclasses import dataclass
from datetime import datetime
import subprocess
import re
import ipaddress
from src.utils.logger import LOGGER as logger

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

def __get_container_ip(container_id: str) -> str:
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
        if not ip:
            raise RuntimeError(f"Invalid IP address found for container {container_id}: {ip}")
        return ip
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to get container IP: {e}")

def __execute_iptables(
    command: List[str],
    check_exists: bool = False
) -> Tuple[bool, str]:
    """
    Execute an iptables (or ip6tables) command.
    Automatically selects iptables for IPv4 and ip6tables for IPv6.

    Args:
        command: List of iptables arguments (e.g., ['-I', 'FORWARD', ...]).
        check_exists: If True, adds '-C' instead of '-I' to check for existing rule.
    Returns:
        Tuple of (success: bool, message: str).
    """
    # Determine if the destination IP in the command is IPv6
    is_ipv6 = False
    try:
        for idx, arg in enumerate(command):
            if arg in ('-d', '--destination') and idx + 1 < len(command):
                dest = command[idx + 1]
                ip_obj = ipaddress.ip_address(dest)
                is_ipv6 = (ip_obj.version == 6)
                break
    except ValueError:
        # If dest isn't a valid IP, assume IPv4 (iptables)
        is_ipv6 = False

    # Select appropriate binary
    binary = 'ip6tables' if is_ipv6 else 'iptables'

    # Adjust action flag if checking existence
    cmd = command.copy()
    if check_exists:
        # Replace first occurrence of '-I' with '-C'
        for i, token in enumerate(cmd):
            if token == '-I':
                cmd[i] = '-C'
                break

    full_cmd = [binary] + cmd + ['-j', 'ACCEPT'] if '-j' not in cmd else [binary] + cmd

    try:
        result = subprocess.run(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=not check_exists
        )
        if result.returncode == 0:
            return True, result.stdout.decode().strip()
        else:
            return False, result.stderr.decode().strip()
    except subprocess.CalledProcessError as e:
        return False, e.stderr.decode().strip()
    except FileNotFoundError:
        return False, f"{binary} not found on system"

def block_all(container_id: str) -> bool:
    """
    Block all outgoing traffic from a specific container.
    """
    try:

        container_ip = __get_container_ip(container_id)
        
        for protocol in Protocol:
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

def allow_connection(container_id: str, ip: str, port: Optional[int] = None, protocol: Protocol = Protocol.TCP) -> bool:
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
        
        success, message = __execute_iptables(command, check_exists=True)
        
        if success:
            logger(f"Allowed {protocol.value} connection from {container_id} to {ip}" +
                (f":{port}" if port else ""))
        else:
            logger(f"Failed to allow connection from {container_id} to {ip}: {message}")
            
        return success
    except Exception as e:
        logger(f"Failed to allow connection: {e}")
        return False
    
def resolve_domain(domain: str) -> List[str]:
    """
    Resolve a domain to its associated IP addresses.
    """
    try:
        return list({info[4][0] for info in socket.getaddrinfo(domain, None)})
    except socket.gaierror:
        raise ValueError(f"Cannot resolve domain: {domain}")
    
def allow_connection_to_domain(container_id: str, domain: str, port: Optional[int] = None, protocol: Protocol = Protocol.TCP) -> bool:
    """
    Allow outgoing traffic from container to all IPs of a domain.
    """
    try:
        ips = resolve_domain(domain)
        results = []
        for ip in ips:
            result = allow_connection(container_id, ip, port, protocol)
            results.append(result)
        return any(results)
    except Exception as e:
        logger(f"Failed to allow connection to domain {domain}: {e}")
        return False


def remove_rule(container_id: str, ip: str, port: Optional[int] = None, protocol: Protocol = Protocol.TCP) -> bool:
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
        
        for protocol in Protocol:
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
