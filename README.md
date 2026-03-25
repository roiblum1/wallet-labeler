# Wallet Label Manager

Manages a label on OpenShift compute nodes to track which nodes contribute resources to the "wallet". Runs as a CronJob and reconciles node labels based on configurable rules.

## What it does

Every run (default: 5 min), the reconciler:

1. Lists all cluster nodes
2. Skips excluded nodes (by role, name regex, or label)
3. For cordoned (unschedulable) nodes → removes the wallet label
4. SSHes into remaining nodes to verify DNS search domains
5. Adds the label to healthy, schedulable compute nodes

## Quick start

```bash
# 1. Test locally (dry-run)
python3 wallet_labeler.py -c config.yaml --dry-run --verbose

# 2. Build container
podman build -t registry.example.com/wallet-labeler:latest -f Containerfile .
podman push registry.example.com/wallet-labeler:latest

# 3. Deploy to OpenShift
oc apply -f k8s/all-in-one.yaml

# 4. Create the SSH key secret (replace with your key)
oc create secret generic node-ssh-key \
  --from-file=id_rsa=/path/to/ssh-key \
  -n wallet-label-manager
```

## Configuration (config.yaml)

All behavior is driven by `config.yaml`. No code changes needed.

### Label

```yaml
label:
  key: "wallet/resource-pool"
  value: "enabled"
```

### Exclusion rules

Nodes matching **any** rule are excluded (label is removed if present).

```yaml
exclude:
  # Regex on node name
  name_regex:
    - "^infra-.*"
    - ".*-gpu-.*"
    - "^worker-0[1-3]$"     # specific nodes

  # Standard OpenShift roles
  roles:
    - "master"
    - "control-plane"
    - "infra"

  # Custom labels (key=value or key-exists)
  labels:
    - "wallet/exclude=true"          # exact match
    - "node.kubernetes.io/special"   # key exists
```

### Examples — common exclusion patterns

**Exclude a single node temporarily:**
```bash
oc label node worker-07 wallet/exclude=true
```

**Exclude all GPU nodes by regex:**
```yaml
name_regex:
  - ".*-gpu-.*"
```

**Exclude nodes with a specific hardware label:**
```yaml
labels:
  - "hardware-type=high-memory"
```

### Search domain verification

```yaml
search_domain:
  enabled: true                           # set false to skip SSH entirely
  expected:
    - "example.corp.local"
  ssh_user: "core"
  ssh_key: "/run/secrets/ssh-key/id_rsa"
  connect_by: "ip"                        # "ip" or "hostname"
```

### Behavior toggles

```yaml
behavior:
  remove_on_cordon: true    # cordoned node → remove label
  remove_on_bad_dns: true   # DNS mismatch → remove label
  dry_run: false            # true = log-only, no changes
```

## Architecture

```
┌──────────────────────────────────────┐
│            CronJob (every 5m)        │
│                                      │
│  ┌────────────┐    ┌──────────────┐  │
│  │ config.yaml│───▶│ wallet_      │  │
│  │ (ConfigMap)│    │ labeler.py   │  │
│  └────────────┘    └──────┬───────┘  │
│                           │          │
│              ┌────────────┼──────────┤
│              │            │          │
│         oc get nodes   SSH to node   │
│         oc label node  verify DNS    │
│              │            │          │
└──────────────┼────────────┼──────────┘
               ▼            ▼
         ┌──────────┐  ┌─────────┐
         │ API      │  │ Node    │
         │ Server   │  │ resolv  │
         └──────────┘  │ .conf   │
                       └─────────┘
```

## Decision flow per node

```
Node
 ├─ excluded by role/regex/label? ──▶ SKIP (remove label if present)
 ├─ cordoned?                     ──▶ REMOVE label
 ├─ DNS check failed?             ──▶ REMOVE label (if configured)
 └─ all OK                        ──▶ ADD label
```

## Project structure

```
wallet-label-manager/
├── config.yaml            # ← edit this, not code
├── wallet_labeler.py      # single-file reconciler
├── Containerfile          # build image
├── k8s/
│   └── all-in-one.yaml   # namespace + RBAC + CronJob
└── README.md
```

## Requirements

- `oc` CLI authenticated (in-cluster via ServiceAccount, or kubeconfig locally)
- `ssh` client + key with access to nodes (only if `search_domain.enabled: true`)
- Python 3.9+ with PyYAML (`pip install pyyaml`)

## Running manually

```bash
# Full run
python3 wallet_labeler.py -c config.yaml

# Dry-run with debug output
python3 wallet_labeler.py -c config.yaml --dry-run --verbose

# Override config path
python3 wallet_labeler.py -c /etc/wallet/config.yaml
```

## Monitoring

The script exits with code 0 on success and 1 on errors. The CronJob's `failedJobsHistoryLimit` keeps the last 5 failures for inspection:

```bash
# Check recent runs
oc get jobs -n wallet-label-manager

# View logs from last run
oc logs -n wallet-label-manager job/$(oc get jobs -n wallet-label-manager \
  --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2)
```
