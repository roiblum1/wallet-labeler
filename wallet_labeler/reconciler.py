import logging

from .node import get_nodes, node_name, node_is_schedulable
from .exclusion import is_excluded
from .dns import check_search_domain, fix_search_domain
from .labels import has_label, add_label, remove_label

log = logging.getLogger("wallet-labeler")


def reconcile(cfg: dict) -> dict:
    label_key = cfg["label"]["key"]
    label_val = cfg["label"]["value"]
    dry_run = cfg.get("behavior", {}).get("dry_run", False)
    remove_on_cordon = cfg.get("behavior", {}).get("remove_on_cordon", True)

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

        # --- DNS check — attempt fix before skipping ---
        dns_ok, dns_detail = check_search_domain(node, cfg)
        if not dns_ok:
            log.warning("%s DNS check failed: %s — attempting fix", name, dns_detail)
            fix_ok, fix_detail = fix_search_domain(node, cfg)
            if fix_ok:
                log.info("%s DNS fix applied: %s — re-verifying", name, fix_detail)
                dns_ok, dns_detail = check_search_domain(node, cfg)

            if not dns_ok:
                log.warning("%s DNS still invalid after fix attempt: %s — skipping", name, dns_detail)
                stats["skipped"] += 1
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
