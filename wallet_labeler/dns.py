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


def fix_search_domain(node: dict, cfg: dict) -> tuple[bool, str]:
    """
    SSH into the node, determine missing search domains, and add them via nmcli.
    Finds the active connection, appends the domains, then reapplies with
    'nmcli device reapply' (non-disruptive — no link drop).
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

    # Step 2: get active connection name and device
    r = subprocess.run(
        ssh_base + ["nmcli -t -f NAME,DEVICE con show --active"],
        capture_output=True, text=True, timeout=timeout + 5,
    )
    if r.returncode != 0:
        return False, f"nmcli query failed: {r.stderr.strip()}"

    conn_name = device = None
    for line in r.stdout.splitlines():
        parts = line.strip().split(":", 1)
        if len(parts) == 2 and parts[1]:
            conn_name, device = parts[0], parts[1]
            break

    if not conn_name:
        return False, "no active nmcli connection found"

    # Step 3: append missing domains to ipv4.dns-search
    domains_arg = " ".join(sorted(missing))
    modify_cmd = f"nmcli con modify '{conn_name}' +ipv4.dns-search '{domains_arg}'"
    r = subprocess.run(
        ssh_base + [modify_cmd],
        capture_output=True, text=True, timeout=timeout + 5,
    )
    if r.returncode != 0:
        return False, f"nmcli modify failed: {r.stderr.strip()}"

    # Step 4: reapply without dropping the link
    reapply_cmd = f"nmcli device reapply '{device}'"
    r = subprocess.run(
        ssh_base + [reapply_cmd],
        capture_output=True, text=True, timeout=timeout + 5,
    )
    if r.returncode != 0:
        return False, f"nmcli reapply failed: {r.stderr.strip()}"

    log.info("fixed search domains on %s (conn=%s device=%s added=%s)",
             host, conn_name, device, domains_arg)
    return True, f"added search domains: {domains_arg}"
