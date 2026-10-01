# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""
Configuration helper for OPC-UA endpoints to handle connectivity issues.
This module provides utilities to configure endpoints that work with different clients.
"""
import socket
from urllib.parse import urlparse
from typing import List, Dict, Optional

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


def _is_docker_interface(interface_name: str) -> bool:
    """Check if interface name looks like a Docker/container internal interface."""
    docker_prefixes = ('docker', 'br-', 'veth', 'cni', 'flannel', 'cali', 'weave')
    return interface_name.lower().startswith(docker_prefixes)


def _is_docker_ip(ip: str) -> bool:
    """
    Check if IP is in a Docker internal network range.

    Docker typically uses:
    - 172.17.0.0/16 for default bridge
    - 172.18-31.0.0/16 for user-defined networks
    - We filter the entire 172.16.0.0/12 range (172.16.x.x - 172.31.x.x)
    """
    if ip.startswith('172.'):
        try:
            second_octet = int(ip.split('.')[1])
            # 172.16.0.0/12 covers 172.16.x.x through 172.31.x.x
            if 16 <= second_octet <= 31:
                return True
        except (ValueError, IndexError):
            pass
    return False


def _get_ips_from_psutil() -> List[str]:
    """Get non-loopback, non-Docker IPs using psutil (preferred method)."""
    if not PSUTIL_AVAILABLE:
        return []

    try:
        non_loopback_ips = []
        for interface_name, addresses in psutil.net_if_addrs().items():
            # Skip Docker/container internal interfaces by name
            if _is_docker_interface(interface_name):
                continue

            for addr in addresses:
                # Only consider IPv4 addresses
                if addr.family == socket.AF_INET:
                    ip = addr.address
                    # Skip loopback and Docker IP ranges
                    if not ip.startswith('127.') and not _is_docker_ip(ip):
                        non_loopback_ips.append(ip)
        return non_loopback_ips
    except Exception:
        return []


def _get_ips_from_socket() -> List[str]:
    """
    Get non-loopback, non-Docker IPs using socket (fallback, no network access required).

    Uses gethostbyname_ex and getaddrinfo to enumerate IPs associated
    with the machine's hostname. Works on Windows MSYS2 and air-gapped systems.
    """
    non_loopback_ips = []

    try:
        # Method 1: gethostbyname_ex returns (hostname, aliaslist, ipaddrlist)
        hostname = socket.gethostname()
        _, _, ip_list = socket.gethostbyname_ex(hostname)
        for ip in ip_list:
            if (not ip.startswith('127.') and
                not _is_docker_ip(ip) and
                ip not in non_loopback_ips):
                non_loopback_ips.append(ip)
    except Exception:
        pass

    try:
        # Method 2: getaddrinfo can find additional addresses
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if (not ip.startswith('127.') and
                not _is_docker_ip(ip) and
                ip not in non_loopback_ips):
                non_loopback_ips.append(ip)
    except Exception:
        pass

    return non_loopback_ips


def _get_ip_from_external_connection() -> Optional[str]:
    """
    Get IP by connecting to external address (last resort, requires network).

    This method determines which interface would be used to reach the internet,
    but requires network connectivity.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith('127.') and not _is_docker_ip(ip):
            return ip
    except Exception:
        pass
    return None


def get_local_ip() -> Optional[str]:
    """
    Get the local IP address of the machine.

    Strategy (in order of preference):
    1. psutil interface enumeration (no network access, most reliable)
    2. socket-based hostname resolution (no network access, works on MSYS2)
    3. External connection test (requires network, determines default route)

    Returns:
        The local IP address string, or None if detection fails
    """
    # Try psutil first (most reliable, no network access required)
    ips = _get_ips_from_psutil()
    if ips:
        return ips[0]

    # Fallback to socket-based detection (no network access, works on MSYS2)
    ips = _get_ips_from_socket()
    if ips:
        return ips[0]

    # Last resort: external connection (requires network)
    return _get_ip_from_external_connection()


def get_available_hostnames() -> List[str]:
    """Get list of available hostnames/IPs for the server."""
    hostnames = ["localhost", "127.0.0.1"]

    try:
        # Add actual hostname
        hostname = socket.gethostname()
        if hostname not in hostnames:
            hostnames.append(hostname)

        # Add FQDN if different
        fqdn = socket.getfqdn()
        if fqdn not in hostnames:
            hostnames.append(fqdn)

        # Add local IP address
        local_ip = get_local_ip()
        if local_ip and local_ip not in hostnames:
            hostnames.append(local_ip)

    except Exception:
        pass

    return hostnames


def normalize_endpoint_url(endpoint_url: str) -> str:
    """
    Normalize endpoint URL for better client compatibility.

    When 0.0.0.0 is used (bind to all interfaces), we need to replace it
    with an actual resolvable address for OPC-UA clients. The priority is:
    1. Network IP (for remote client access)
    2. Hostname (fallback)
    3. localhost (last resort, only works for local clients)
    """
    parsed = urlparse(endpoint_url)

    # If using 0.0.0.0, replace with a resolvable address
    if parsed.hostname == "0.0.0.0":
        # Try to get the network IP first (best for remote access)
        network_ip = get_local_ip()
        if network_ip:
            return f"{parsed.scheme}://{network_ip}:{parsed.port}{parsed.path}"

        # Fallback to hostname
        try:
            hostname = socket.gethostname()
            if hostname and hostname != "localhost":
                return f"{parsed.scheme}://{hostname}:{parsed.port}{parsed.path}"
        except Exception:
            pass

        # Last resort: use localhost (only works for local clients)
        return f"{parsed.scheme}://localhost:{parsed.port}{parsed.path}"

    return endpoint_url


def create_multiple_endpoints(base_endpoint: str) -> List[str]:
    """Create multiple endpoint variations for better connectivity."""
    parsed = urlparse(base_endpoint)
    endpoints = []
    
    hostnames = get_available_hostnames()
    
    for hostname in hostnames:
        endpoint = f"{parsed.scheme}://{hostname}:{parsed.port}{parsed.path}"
        if endpoint not in endpoints:
            endpoints.append(endpoint)
    
    return endpoints


def suggest_client_endpoints(server_endpoint: str) -> Dict[str, str]:
    """Suggest different endpoint URLs for different client scenarios."""
    parsed = urlparse(server_endpoint)
    
    return {
        "local_connection": f"opc.tcp://localhost:{parsed.port}{parsed.path}",
        "same_machine": f"opc.tcp://127.0.0.1:{parsed.port}{parsed.path}",
        "network_hostname": f"opc.tcp://{socket.gethostname()}:{parsed.port}{parsed.path}",
        "network_ip": f"opc.tcp://{get_local_ip()}:{parsed.port}{parsed.path}" if get_local_ip() else None
    }


def validate_endpoint_format(endpoint_url: str) -> bool:
    """Validate if endpoint URL has correct OPC-UA format."""
    try:
        parsed = urlparse(endpoint_url)
        return (
            parsed.scheme == "opc.tcp" and
            parsed.hostname is not None and
            parsed.port is not None and
            len(parsed.path) > 0
        )
    except:
        return False