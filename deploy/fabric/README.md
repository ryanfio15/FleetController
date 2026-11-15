# FABRIC provisioning

One script does the whole thing — slice, RKE2, dependencies, application:

```bash
python deploy/fabric/fleetctl.py check     # read-only; creates nothing
python deploy/fabric/fleetctl.py up        # the rest
```

`up` is safe to re-run. It reuses an existing slice, skips RKE2 where it is
already installed, and re-applies the manifests.

> **Not yet run against the real testbed.** The script is written to fablib's
> documented API, but FABRIC-side behaviour — site capacity, image names, how
> long post-boot config takes — could not be verified without a slice. That is
> what `check` is for: it talks to FABRIC read-only and creates nothing, so a
> wrong API call surfaces there at no cost. Expect to iterate once on the first
> real run.

## Setup, once

**1. A virtualenv.** fablib pins a lot and supports a narrower Python range
than the newest release, so don't install it into system Python:

```bash
python -m venv .venv-fabric
source .venv-fabric/Scripts/activate        # Git Bash on Windows
pip install -r deploy/fabric/requirements.txt
```

**2. A token and `~/.fabric_rc`.** Download a fresh token from the FABRIC
portal, then create `~/.fabric_rc`:

```sh
export FABRIC_PROJECT_ID=<your project GUID>
export FABRIC_TOKEN_LOCATION=$HOME/.fabric/token.json
export FABRIC_BASTION_USERNAME=<portal username, e.g. ryanfio15_0000012345>
export FABRIC_BASTION_KEY_LOCATION=$HOME/.ssh/fabric_bastion_key
export FABRIC_SLICE_PRIVATE_KEY_FILE=$HOME/.ssh/fabric_sliver_key
export FABRIC_SLICE_PUBLIC_KEY_FILE=$HOME/.ssh/fabric_sliver_key.pub
```

The script sources this file itself — a plain `python fleetctl.py` in a terminal
does not pick it up the way a Jupyter kernel does, which is the usual reason
something works in a notebook and not here. Missing settings are reported by
name before anything is created.

**Tokens expire.** A 401 or 403 from `check` means download a new one; it does
not mean anything is misconfigured.

## What `up` does

| Stage | What happens |
|---|---|
| `slice` | 5 VMs at one site, one L3 FABNetv4 network, addresses and a route to `10.128.0.0/10` |
| `rke2` | server on cp-1, agents on the other four, each with its `tier=` node label and `tls-san` covering the tunnel |
| `kubeconfig` | fetches cp-1's kubeconfig to `deploy/fabric/kubeconfig` (gitignored) |
| `deps` | cert-manager, cert-manager-csi-driver, KEDA — via `./fc.sh k8s-deps`, so versions are pinned in one place |
| `app` | generates the `fleet-db` secret, renders the fabric overlay, applies it |

Run one stage at a time with `--only`:

```bash
python deploy/fabric/fleetctl.py up --only deps,app
```

### Node layout

Matches [plan §10](../../docs/TECHNICAL_PLAN.md) and the PDF's Chapter 2. Edit
`Config.nodes` in the script to change it.

| Node | `tier` | cores | RAM | disk | gets |
|---|---|---|---|---|---|
| cp-1 | `control` | 4 | 8 GB | 50 GB | control plane |
| edge-1 | `edge` | 2 | 4 GB | 20 GB | ingest (NodePort 30443) |
| worker-1 | `pipeline` | 4 | 8 GB | 40 GB | processor, sims, grafana |
| worker-2 | `pipeline` | 4 | 8 GB | 40 GB | ″ |
| state-1 | `state` | 4 | 16 GB | 100 GB | nats, timescaledb, migrate, pg-backup |
| | | **18** | **44 GB** | **250 GB** | |

If your project quota is smaller than that, trim `Config.nodes` — the tiers are
what the overlay pins to, not the sizes. `check` reports the request and finds a
site with capacity before anything is created.

## The tunnel

FABRIC has no cloud load balancer, so external entry is NodePort through the
bastion. The fetched kubeconfig already points at `127.0.0.1:6443`, which is
what a tunnel wants:

```bash
python deploy/fabric/fleetctl.py tunnel     # prints the exact commands
```

Leave it running in another terminal. `deps` and `app` need it, which is why
`up` prints it and waits.

## Afterwards

```bash
export KUBECONFIG=deploy/fabric/kubeconfig
./fc.sh k8s-status
./fc.sh k8s-verify        # the Stage 0 exit criterion, on FABRIC
```

Grafana: `http://localhost:30080/grafana/d/fc-fleet-overview`

Leases are short relative to a 14-week project:

```bash
python deploy/fabric/fleetctl.py renew --days 14
python deploy/fabric/fleetctl.py destroy          # confirms first
```

## Two values you do not have to supply

The Kubernetes overlay needs edge-1's address and the FABNetv4 subnet.
**`fleetctl.py` reads both back from the live slice** and substitutes them into
the rendered manifests, leaving `site.yaml` as a committed template.

That is deliberate rather than convenient. Typing edge-1's address by hand is
how you get a cluster where every pod is Running, every probe is green, and
every TLS handshake fails — because the address the sims dial is not in the
server certificate's SANs. Discovering it removes the chance to get it wrong.

## Secrets

`app` generates the `fleet-db` password with `secrets.token_urlsafe` and creates
the Secret directly, so it never touches disk. The tradeoff is that the
manifests are not reproducible from the repository alone. If you want that
instead, seal it and uncomment the entry in
[the overlay's kustomization](../k8s/overlays/fabric/kustomization.yaml) — the
commands are in [that README](../k8s/overlays/fabric/README.md).

## Deliberate single point of failure

`nats-0` and `timescaledb-0` pin to state-1 with node-local `local-path`
volumes. Losing that node means restoring from the `pg-backup` CronJob, and
`destroy` takes the database with it. This is accepted by design — the
redundancy story is JetStream replay, not database replication (plan §10). Worth
stating plainly in the write-up rather than letting a reviewer find it.
