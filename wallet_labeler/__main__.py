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
import logging
import sys

from .config import load_config
from .reconciler import reconcile

log = logging.getLogger("wallet-labeler")


def main():
    parser = argparse.ArgumentParser(description="Wallet Label Manager for OpenShift nodes")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Override: enable dry-run")
    parser.add_argument("-v", "--verbose", action="store_true", help="Override: DEBUG logging")
    args = parser.parse_args()

    cfg = load_config(args.config)

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
