import logging

from .node import oc, node_labels

log = logging.getLogger("wallet-labeler")


def has_label(node: dict, key: str, value: str) -> bool:
    return node_labels(node).get(key) == value


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
