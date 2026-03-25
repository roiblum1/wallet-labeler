import re

from .node import node_name, node_labels


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
