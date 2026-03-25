import json
import logging
import subprocess

log = logging.getLogger("wallet-labeler")


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
