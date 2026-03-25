#!/usr/bin/env python3
"""
Wallet Label Manager
Manages a label on OpenShift compute nodes based on:
  - Node role (exclude masters/infra)
  - Exclusion rules (regex, labels)
  - Scheduling state (cordoned nodes lose the label)
  - DNS search domain verification via SSH
"""

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log = logging.getLogger("wallet-labeler")


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def oc(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run an oc command and return the result."""
    cmd = ["oc"] + args
    log.debug("exec: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def get_nodes() -> list[dict]:
    """Fetch all nodes as JSON."""
    r = oc(["get", "nodes", "-o", "json"])
    return json.loads(r.stdout)["items"]


def node_name(node: dict) -> str:
    return node["metadata"]["name"]


def node_labels(node: dict) -> dict:
    return node["metadata"].get("labels", {})


def node_is_schedulable(node: dict) -> bool:
    return not node.get("spec", {}).get("unschedulable", False)


def node_address(node: dict, addr_type: str = "InternalIP") -> str | None:
    """Get a node address by type (InternalIP or Hostname)."""
    for addr in node.get("status", {}).get("addresses", []):
        if addr["type"] == addr_type:
            return addr["address"]
    return None


# ---------------------------------------------------------------------------
# Exclusion logic
# ---------------------------------------------------------------------------

def is_excluded(node: dict, cfg: dict) -> tuple[bool, str]:
    """
    Check if a node should be excluded.
    Returns (excluded: bool, reason: str).
    """
    name = node_name(node)
    labels = node_labels(node)
    excl = cfg.get("exclude", {})

    # --- Role exclusion ---
    for role in excl.get("roles", []):
        role_key = f"node-role.kubernetes.io/{role}"
        if role_key in labels:
            return True, f"has role '{role}'"

    # --- Name regex exclusion ---
    for pattern in excl.get("name_regex", []):
        if re.search(pattern, name):
            return True, f"name matches regex '{pattern}'"

    # --- Label exclusion ---
    for label_expr in excl.get("labels", []):
        if "=" in label_expr:
            key, val = label_expr.split("=", 1)
            if labels.get(key) == val:
                return True, f"has label {label_expr}"
        else:
            if label_expr in labels:
                return True, f"has label key '{label_expr}'"

    return False, ""


# ---------------------------------------------------------------------------
# SSH search domain check
# ---------------------------------------------------------------------------

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

    ssh_user = sd_cfg.get("ssh_user", "core")
    ssh_key = sd_cfg.get("ssh_key", "")
    timeout = str(sd_cfg.get("ssh_timeout", 10))

    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "LogLevel=ERROR",
    ]
    if ssh_key:
        ssh_cmd += ["-i", ssh_key]
    ssh_cmd += [f"{ssh_user}@{host}", "cat /etc/resolv.conf"]

    try:
        r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=int(timeout) + 5)
    except subprocess.TimeoutExpired:
        return False, f"SSH timeout to {host}"

    if r.returncode != 0:
        return False, f"SSH failed ({r.returncode}): {r.stderr.strip()}"

    # Parse search domains from resolv.conf
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


# ---------------------------------------------------------------------------
# Label management
# ---------------------------------------------------------------------------

def add_label(name: str, key: str, value: str, dry_run: bool) -> bool:
    label = f"{key}={value}"
    if dry_run:
        log.info("[DRY-RUN] would add label %s to %s", label, name)
        return True
    r = oc(["label", "node", name, label, "--overwrite"], check=False)
    if r.returncode == 0:
        log.info("added label %s to %s", label, name)
        return True
    log.error("failed to label %s: %s", name, r.stderr.strip())
    return False


def remove_label(name: str, key: str, dry_run: bool) -> bool:
    if dry_run:
        log.info("[DRY-RUN] would remove label %s from %s", key, name)
        return True
    r = oc(["label", "node", name, f"{key}-"], check=False)
    if r.returncode == 0:
        log.info("removed label %s from %s", key, name)
        return True
    log.error("failed to remove label from %s: %s", name, r.stderr.strip())
    return False


def has_label(node: dict, key: str, value: str) -> bool:
    return node_labels(node).get(key) == value


# ---------------------------------------------------------------------------
# Main reconciliation loop
# ---------------------------------------------------------------------------

def reconcile(cfg: dict) -> dict:
    label_key = cfg["label"]["key"]
    label_val = cfg["label"]["value"]
    dry_run = cfg.get("behavior", {}).get("dry_run", False)
    remove_on_cordon = cfg.get("behavior", {}).get("remove_on_cordon", True)
    remove_on_bad_dns = cfg.get("behavior", {}).get("remove_on_bad_dns", True)

    stats = {"total": 0, "excluded": 0, "labeled": 0, "unlabeled": 0, "skipped": 0, "errors": 0}

    nodes = get_nodes()
    stats["total"] = len(nodes)

    for node in nodes:
        name = node_name(node)
        currently_labeled = has_label(node, label_key, label_val)

        # --- Exclusion check ---
        excluded, reason = is_excluded(node, cfg)
        if excluded:
            log.debug("SKIP %s (excluded: %s)", name, reason)
            stats["excluded"] += 1
            if currently_labeled:
                log.info("%s is excluded (%s) but has label — removing", name, reason)
                remove_label(name, label_key, dry_run)
                stats["unlabeled"] += 1
            continue

        # --- Cordon check ---
        if not node_is_schedulable(node):
            stats["skipped"] += 1
            if currently_labeled and remove_on_cordon:
                log.info("%s is cordoned — removing label", name)
                remove_label(name, label_key, dry_run)
                stats["unlabeled"] += 1
            else:
                log.debug("SKIP %s (cordoned, no label)", name)
            continue

        # --- DNS check ---
        dns_ok, dns_detail = check_search_domain(node, cfg)
        if not dns_ok:
            stats["skipped"] += 1
            log.warning("%s DNS check failed: %s", name, dns_detail)
            if currently_labeled and remove_on_bad_dns:
                log.info("%s has bad DNS — removing label", name)
                remove_label(name, label_key, dry_run)
                stats["unlabeled"] += 1
            continue

        # --- All good → ensure label ---
        if not currently_labeled:
            if add_label(name, label_key, label_val, dry_run):
                stats["labeled"] += 1
            else:
                stats["errors"] += 1
        else:
            log.debug("OK %s (already labeled)", name)

    return stats


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Wallet Label Manager for OpenShift nodes")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Override: enable dry-run")
    parser.add_argument("-v", "--verbose", action="store_true", help="Override: DEBUG logging")
    args = parser.parse_args()

    cfg = load_config(args.config)

    # Setup logging
    level = "DEBUG" if args.verbose else cfg.get("log_level", "INFO")
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.dry_run:
        cfg.setdefault("behavior", {})["dry_run"] = True

    dry_label = " [DRY-RUN]" if cfg.get("behavior", {}).get("dry_run") else ""
    log.info("=== Wallet Label Manager starting%s ===", dry_label)
    log.info("Label: %s=%s", cfg["label"]["key"], cfg["label"]["value"])

    try:
        stats = reconcile(cfg)
    except Exception:
        log.exception("reconciliation failed")
        sys.exit(1)

    log.info(
        "Done — total=%d excluded=%d labeled=%d unlabeled=%d skipped=%d errors=%d",
        stats["total"], stats["excluded"], stats["labeled"],
        stats["unlabeled"], stats["skipped"], stats["errors"],
    )

    sys.exit(1 if stats["errors"] > 0 else 0)


if __name__ == "__main__":
    main()
