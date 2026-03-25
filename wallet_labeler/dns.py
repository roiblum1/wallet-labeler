import logging
import subprocess

from .node import node_address

log = logging.getLogger("wallet-labeler")


def _build_ssh_base(sd_cfg: dict, host: str) -> list[str]:
    timeout = str(sd_cfg.get("ssh_timeout", 10))
    ssh_user = sd_cfg.get("ssh_user", "core")
    ssh_key = sd_cfg.get("ssh_key", "")
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "LogLevel=ERROR",
    ]
    if ssh_key:
        cmd += ["-i", ssh_key]
    cmd.append(f"{ssh_user}@{host}")
    return cmd


def check_search_domain(node: dict, cfg: dict) -> tuple[bool, str]:
    """
    SSH into the node and verify /etc/resolv.conf search domains.
    Returns (ok: bool, detail: str).
    """
    sd_cfg = cfg.get("search_domain", {})
    if not sd_cfg.get("enabled", True):
        return True, "check disabled"

    addr_type = "InternalIP" if sd_cfg.get("connect_by", "ip") == "ip" else "Hostname"
    host = node_address(node, addr_type)
    if not host:
        return False, f"no {addr_type} address found"

    timeout = int(sd_cfg.get("ssh_timeout", 10))
    ssh_cmd = _build_ssh_base(sd_cfg, host) + ["cat /etc/resolv.conf"]

    try:
        r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return False, f"SSH timeout to {host}"

    if r.returncode != 0:
        return False, f"SSH failed ({r.returncode}): {r.stderr.strip()}"

    found_domains: set[str] = set()
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("search "):
            found_domains.update(line.split()[1:])

    expected = set(sd_cfg.get("expected", []))
    missing = expected - found_domains
    if missing:
        return False, f"missing search domains: {', '.join(sorted(missing))}"

    return True, "search domains OK"


def _get_node_vendor(ssh_base: list[str], timeout: int) -> str:
    """Read the DMI sys_vendor string from the node. Returns empty string on failure."""
    r = subprocess.run(
        ssh_base + ["cat /sys/class/dmi/id/sys_vendor"],
        capture_output=True, text=True, timeout=timeout + 5,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def _get_interface_for_vendor(vendor: str, sd_cfg: dict) -> str:
    """
    Map a DMI vendor string to a network interface name using the
    'vendor_interfaces' config block. Matching is case-insensitive substring.
    Falls back to the 'default' key, then 'bond0'.
    """
    vendor_map: dict = sd_cfg.get("vendor_interfaces", {})
    vendor_lower = vendor.lower()
    for key, iface in vendor_map.items():
        if key == "default":
            continue
        if key.lower() in vendor_lower:
            return iface
    return vendor_map.get("default", "bond0")


def fix_search_domain(node: dict, cfg: dict) -> tuple[bool, str]:
    """
    SSH into the node, determine missing search domains, and add them via nmcli.
    Detects the node vendor from DMI to pick the correct network interface,
    then appends missing domains and reapplies with 'nmcli device reapply'
    (non-disruptive — no link drop).
    Returns (ok: bool, detail: str).
    """
    sd_cfg = cfg.get("search_domain", {})
    addr_type = "InternalIP" if sd_cfg.get("connect_by", "ip") == "ip" else "Hostname"
    host = node_address(node, addr_type)
    if not host:
        return False, f"no {addr_type} address found"

    timeout = int(sd_cfg.get("ssh_timeout", 10))
    ssh_base = _build_ssh_base(sd_cfg, host)

    # Step 1: determine which domains are missing from /etc/resolv.conf
    r = subprocess.run(
        ssh_base + ["cat /etc/resolv.conf"],
        capture_output=True, text=True, timeout=timeout + 5,
    )
    if r.returncode != 0:
        return False, f"SSH failed reading resolv.conf: {r.stderr.strip()}"

    found_domains: set[str] = set()
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("search "):
            found_domains.update(line.split()[1:])

    expected = set(sd_cfg.get("expected", []))
    missing = expected - found_domains
    if not missing:
        return True, "search domains already correct"

    # Step 2: detect vendor and resolve target interface
    vendor = _get_node_vendor(ssh_base, timeout)
    iface = _get_interface_for_vendor(vendor, sd_cfg)
    log.debug("vendor=%r → interface=%s", vendor, iface)

    # Step 3: find the active nmcli connection bound to that interface
    r = subprocess.run(
        ssh_base + ["nmcli -t -f NAME,DEVICE con show --active"],
        capture_output=True, text=True, timeout=timeout + 5,
    )
    if r.returncode != 0:
        return False, f"nmcli query failed: {r.stderr.strip()}"

    conn_name = None
    for line in r.stdout.splitlines():
        parts = line.strip().split(":", 1)
        if len(parts) == 2 and parts[1] == iface:
            conn_name = parts[0]
            break

    if not conn_name:
        return False, f"no active nmcli connection found for interface '{iface}' (vendor={vendor!r})"

    # Step 4: append missing domains to ipv4.dns-search
    domains_arg = " ".join(sorted(missing))
    modify_cmd = f"nmcli con modify '{conn_name}' +ipv4.dns-search '{domains_arg}'"
    r = subprocess.run(
        ssh_base + [modify_cmd],
        capture_output=True, text=True, timeout=timeout + 5,
    )
    if r.returncode != 0:
        return False, f"nmcli modify failed: {r.stderr.strip()}"

    # Step 5: reapply without dropping the link
    reapply_cmd = f"nmcli device reapply '{iface}'"
    r = subprocess.run(
        ssh_base + [reapply_cmd],
        capture_output=True, text=True, timeout=timeout + 5,
    )
    if r.returncode != 0:
        return False, f"nmcli reapply failed: {r.stderr.strip()}"

    log.info("fixed search domains on %s (vendor=%r iface=%s conn=%s added=%s)",
             host, vendor, iface, conn_name, domains_arg)
    return True, f"added search domains: {domains_arg}"
