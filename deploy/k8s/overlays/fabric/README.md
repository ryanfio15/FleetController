# FABRIC overlay

Stage 3 of [the plan](../../../../docs/TECHNICAL_PLAN.md). RKE2 on five FABRIC
VMs, reached through the SSH bastion.

**You normally do not apply this overlay by hand.**
[`deploy/fabric/fleetctl.py`](../../../fabric/README.md) provisions the slice,
installs RKE2, and applies this overlay with the site's real values substituted
in:

```bash
python deploy/fabric/fleetctl.py up
```

The rest of this file is what that script does on your behalf, and what to do
when you want to drive a step yourself.

## The two placeholders

| Placeholder | What it is | Where it is used |
|---|---|---|
| `EDGE1_ADDR` | edge-1's FABNetv4 address | `SIM_INGEST_URL`, and the ingest server cert's SANs — the sims address ingest by IP, so the cert must contain it or every handshake fails hostname verification |
| `NODE_CIDR` | the slice's FABNetv4 subnet, e.g. `10.132.4.0/24` | the `ipBlock` letting sims reach edge-1's NodePort — that hop is node-network traffic, so no `podSelector` can describe it |

`fleetctl.py` reads both back from the live slice, so `site.yaml` stays a
committed template. Applying by hand means filling them in; they are invalid on
purpose so an unfilled one fails at `kubectl apply` naming the field.

```bash
grep -n 'EDGE1_ADDR\|NODE_CIDR' site.yaml
```

## The database secret

`fleetctl.py` generates the password and creates the Secret directly, so it
never touches disk. Prefer sealed-secrets if you want the manifests
reproducible from the repository alone:

```bash
kubectl create secret generic fleet-db --dry-run=client -o yaml \
  --from-literal=POSTGRES_USER=fleet \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -base64 24)" \
  --from-literal=POSTGRES_DB=fleet \
| kubeseal --format yaml --namespace fleet > fleet-db-sealed.yaml
```

Then uncomment the `fleet-db-sealed.yaml` entry in `kustomization.yaml`. A
SealedSecret is encrypted to this cluster's own key, so unlike `secret.env` it is
safe to commit — which is the whole point here, since FABRIC is the environment
that has to be reproducible from the repository alone (plan §7.3).

Generate a fresh password rather than reusing `.env`'s `devpassword`.

## Node labels

`placement.yaml` needs no editing, but it pins workloads to tiers that the RKE2
install must have set — `fleetctl.py` writes them into each node's
`/etc/rancher/rke2/config.yaml` as `node-label`. A node that comes up without
its label leaves pods Pending with no other symptom, so `up` prints
`kubectl get nodes -L tier` once the cluster forms.

| Node | Label | Gets |
|---|---|---|
| cp-1 | `tier=control` | — (control plane) |
| edge-1 | `tier=edge` | ingest |
| worker-1, worker-2 | `tier=pipeline` | processor, sims, grafana |
| state-1 | `tier=state` | nats, timescaledb, migrate, pg-backup |

```bash
kubectl get nodes -L tier     # confirm before applying
```

## Deploying

```bash
python deploy/fabric/fleetctl.py up --only app
```

`./fc.sh k8s-apply` deliberately **refuses** when `K8S_OVERLAY=fabric`: it has
no way to fill the placeholders, and applying them stale is how you get a
cluster whose server certificate does not cover the address its devices dial.
The read-only commands (`k8s-status`, `k8s-verify`, `k8s-psql`, `k8s-logs`,
`k8s-scale`) work fine against FABRIC with `KUBECONFIG` set.

Images come from GHCR rather than being side-loaded. If the FABRIC VMs turn out
to have no usable outbound internet, the fallback is `docker save` → `scp`
through the bastion → `rke2 ctr images import` on each node.

`fleetctl.py` also applies `ingress-nginx-nodeport.yaml`, which reconfigures the
ingress-nginx RKE2 already ships from hostPorts 80/443 to NodePort 30080. That
file targets `kube-system`, so it cannot live inside this namespaced overlay.

## Tunnels

FABRIC has no cloud load balancer, so external entry is NodePort through the
bastion (PDF, Services and Scaling).

```bash
# kubectl
ssh -J <bastion-user>@bastion.fabric-testbed.net -L 6443:localhost:6443 ubuntu@<cp-1>
# ingress-nginx, for the browser
ssh -J <bastion-user>@bastion.fabric-testbed.net -L 30080:localhost:30080 ubuntu@<edge-1>
```

The kubeconfig at `/etc/rancher/rke2/rke2.yaml` on cp-1 points at `127.0.0.1`,
which is exactly what a tunnel wants. Fetch it to `deploy/fabric/kubeconfig`
(gitignored).

## Things that are genuinely new here

Everything else was rehearsed on kind. What could not be:

- Slice provisioning and the FABNetv4 network
- Node labels and `nodeSelector` pinning actually mattering
- `local-path` volumes being node-local — which is why the data tier pins to
  state-1 and stays there, and why **the data tier is a single point of failure
  by design** (plan §10). The redundancy story is JetStream replay and the
  pg-backup CronJob, not replication. Say that in the write-up rather than
  letting a reviewer find it.
- sealed-secrets
- The bastion tunnel
- Real inter-node latency

## Stage 3 exit criterion

Everything in [the kind verification list](../../README.md#verification),
through the tunnel, plus:

```bash
kubectl drain worker-1 --ignore-daemonsets --delete-emptydir-data   # ingest stays up
kubectl -n fleet delete pod nats-0                                   # no data loss
```
