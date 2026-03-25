# Wallet Label Manager

Manages a label on OpenShift compute nodes to track which nodes contribute resources to the "wallet". Runs as a Kubernetes CronJob and reconciles node labels based on configurable rules.

---

## What it does

Every run (default: every 5 minutes), the reconciler:

1. Lists all cluster nodes
2. Skips excluded nodes (by role, name regex, or label) — removes the label if they somehow have it
3. Skips cordoned (unschedulable) nodes → removes the label
4. SSHes into remaining nodes to verify DNS search domains
   - If a domain is missing, it **automatically fixes it** via `nmcli` (no manual intervention needed)
   - The target network interface is selected based on the node's hardware vendor (VMware, Red Hat, or fallback)
5. Adds the label to healthy, schedulable compute nodes with correct DNS

---

## Decision flow per node

```text
Node
 ├─ excluded by role / name regex / label? ──▶ SKIP  (remove label if present)
 ├─ cordoned?                               ──▶ REMOVE label
 ├─ DNS search domain missing?
 │    ├─ detect vendor → pick interface
 │    ├─ fix via nmcli on the node
 │    └─ re-verify → still wrong? ──▶ SKIP  (label kept as-is)
 └─ all OK                                  ──▶ ADD label
```

---

## Project structure

```text
wallet-labeler/
├── config.yaml                  # local config for development/testing
├── Containerfile                # container image build
├── all-in-one.yaml              # flat Kubernetes manifest (alternative to Helm)
│
├── wallet_labeler/              # Python package
│   ├── __main__.py              # CLI entry point (argparse + logging setup)
│   ├── config.py                # config file loader
│   ├── node.py                  # oc client + node helper functions
│   ├── exclusion.py             # exclusion rule evaluation
│   ├── dns.py                   # SSH search domain check + nmcli auto-fix
│   ├── labels.py                # label add / remove / check
│   └── reconciler.py           # main reconciliation loop
│
└── chart/
    └── wallet-labeler/          # Helm chart
        ├── Chart.yaml
        ├── values.yaml          # all defaults here
        └── templates/
            ├── namespace.yaml
            ├── serviceaccount.yaml
            ├── rbac.yaml
            ├── secret.yaml
            ├── configmap.yaml
            └── cronjob.yaml
```

---

## Quick start

### Local dry-run (no cluster needed)

```bash
pip install pyyaml

# Preview what would happen — no changes made
python3 -m wallet_labeler -c config.yaml --dry-run --verbose
```

### Build and push container

```bash
podman build -t registry.example.com/wallet-labeler:latest -f Containerfile .
podman push registry.example.com/wallet-labeler:latest
```

### Deploy with Helm

```bash
# Install (SSH key is base64-encoded)
helm install wallet-labeler ./chart/wallet-labeler \
  --set sshPrivateKey="$(base64 -w0 ~/.ssh/id_rsa)" \
  --set config.search_domain.expected[0]="your.domain.local"

# Upgrade after config change
helm upgrade wallet-labeler ./chart/wallet-labeler -f my-values.yaml

# Uninstall
helm uninstall wallet-labeler
```

### Deploy with plain manifests

```bash
oc apply -f all-in-one.yaml

# Provide the SSH key
oc create secret generic node-ssh-key \
  --from-file=id_rsa=/path/to/ssh-key \
  -n wallet-label-manager
```

---

## Configuration reference

All behavior is driven by `config.yaml` (or the `config` block in `values.yaml` for Helm). No code changes needed.

### Label

```yaml
label:
  key: "wallet/resource-pool"
  value: "enabled"
```

### Search domain verification and auto-fix

```yaml
search_domain:
  enabled: true             # set false to skip SSH entirely
  expected:
    - "example.corp.local"
  ssh_user: "core"
  ssh_key: "/run/secrets/ssh-key/id_rsa"
  ssh_timeout: 10           # seconds
  connect_by: "ip"          # "ip" → InternalIP, "hostname" → Hostname

  # Interface selected by hardware vendor (DMI sys_vendor, case-insensitive substring match)
  vendor_interfaces:
    vmware: "ens192"        # VMware, Inc.
    red hat: "eth0"         # Red Hat / oVirt / RHEV
    default: "bond0"        # bare metal or unknown
```

When a search domain is missing the labeler will:

1. Read `/sys/class/dmi/id/sys_vendor` on the node to detect the vendor
2. Pick the matching interface from `vendor_interfaces`
3. Run `nmcli con modify <conn> +ipv4.dns-search <domain>` on that interface's connection
4. Run `nmcli device reapply <iface>` to apply without dropping the link
5. Re-verify — if still wrong, the node is skipped this run (label is left untouched)

### Exclusion rules

A node is excluded if it matches **any** rule. Excluded nodes have the label removed if they carry it.

```yaml
exclude:
  # Regex patterns matched against node name
  name_regex:
    - "^infra-.*"
    - ".*-gpu-.*"

  # Nodes with any of these node-role labels
  roles:
    - "master"
    - "control-plane"
    - "infra"

  # Nodes carrying any of these labels (key=value exact match, or key-exists)
  labels:
    - "wallet/exclude=true"
    - "node.kubernetes.io/special"
```

**Temporarily exclude a single node:**

```bash
oc label node worker-07 wallet/exclude=true
# Re-include it
oc label node worker-07 wallet/exclude-
```

### Behavior toggles

```yaml
behavior:
  remove_on_cordon: true   # cordoned node → remove label
  dry_run: false           # true = log only, no changes applied
```

### Logging

```yaml
log_level: "INFO"   # DEBUG | INFO | WARNING | ERROR
```

---

## Running manually

```bash
# Full run
python3 -m wallet_labeler -c config.yaml

# Dry-run with debug output
python3 -m wallet_labeler -c config.yaml --dry-run --verbose

# Custom config path
python3 -m wallet_labeler -c /etc/wallet/config.yaml
```

---

## Requirements

| Requirement | Notes |
| --- | --- |
| Python 3.10+ | With `pyyaml` (`pip install pyyaml`) |
| `oc` CLI | Authenticated — in-cluster via ServiceAccount, or local kubeconfig |
| `ssh` client + key | Only needed when `search_domain.enabled: true` |
| `nmcli` on nodes | Used for DNS auto-fix — present by default on RHCOS/CoreOS |

---

## Monitoring

The process exits `0` on success and `1` if any labeling errors occurred. The CronJob keeps the last 3 successful and 5 failed job pods for inspection.

```bash
# List recent job runs
oc get jobs -n wallet-label-manager

# View logs from the latest run
oc logs -n wallet-label-manager \
  job/$(oc get jobs -n wallet-label-manager \
    --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2)

# Watch live
oc logs -n wallet-label-manager -f \
  job/$(oc get jobs -n wallet-label-manager \
    --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2)
```

**Sample output:**

```text
2026-03-25 10:00:01 [INFO   ] === Wallet Label Manager starting ===
2026-03-25 10:00:01 [INFO   ] Label: wallet/resource-pool=enabled
2026-03-25 10:00:02 [INFO   ] added label wallet/resource-pool=enabled to worker-01
2026-03-25 10:00:03 [WARNING] worker-04 DNS check failed: missing search domains: example.corp.local — attempting fix
2026-03-25 10:00:04 [INFO   ] fixed search domains on 192.168.1.14 (vendor='VMware, Inc.' iface=ens192 conn=ens192 added=example.corp.local)
2026-03-25 10:00:05 [INFO   ] Done — total=12 excluded=3 labeled=1 unlabeled=0 skipped=0 errors=0
```
