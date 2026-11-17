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
than the newest release, so don't install it into system Python, and name the
interpreter rather than taking whatever `python` happens to be:

```powershell
# PowerShell
py -3.12 -m venv .venv-fabric
.\.venv-fabric\Scripts\Activate.ps1
python -m pip install -r deploy\fabric\requirements.txt
```

```bash
# Git Bash / Linux / macOS
py -3.12 -m venv .venv-fabric       # or python3.12 -m venv .venv-fabric
source .venv-fabric/Scripts/activate
pip install -r deploy/fabric/requirements.txt
```

`source` is a bash builtin — in PowerShell it is `.\...\Activate.ps1`, and a
leading `/` on the path points at the drive root rather than here. If
PowerShell refuses to run the script at all, the execution policy is blocking
it: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`.

**Check the version took.** `python --version` inside the venv has to say
3.12.x. `py -3.12` does not always bind the version it is given, and a venv
built on the wrong interpreter still installs fablib and still imports it —
it just takes the failure later, mid-provision. `cat .venv-fabric/pyvenv.cfg`
names the real base interpreter. To pin it beyond doubt:

```bash
py -3.12 -c "import sys; print(sys.executable)"   # then pass that path to -m venv
```

**2. A token and `~/.fabric_rc`.** Download a fresh token from the FABRIC
portal, then copy [`fabric_rc.example`](fabric_rc.example) to `~/.fabric_rc`
and fill in the two empty values — your project GUID and your bastion
username. Everything else is a path.

```sh
cp deploy/fabric/fabric_rc.example ~/.fabric_rc
```

The script sources this file itself — a plain `python fleetctl.py` in a terminal
does not pick it up the way a Jupyter kernel does, which is the usual reason
something works in a notebook and not here. Missing settings are reported by
name before anything is created.

**Write the paths absolute.** `load_fabric_rc` assigns the values literally
rather than running a shell, and the later `expanduser()` only handles `~`, so
a `$HOME/...` path reaches `Path()` as the four characters `$HOME` and fails
the file check naming a filename with a dollar sign in it.

**3. Two keypairs.** One authenticates you to `bastion.fabric-testbed.net`;
the other is injected into each VM's `authorized_keys` when the slice is
created. Neither can be a passphrase-protected key, because the script drives
SSH non-interactively.

```sh
ssh-keygen -t rsa -b 3072 -N "" -f ~/.ssh/fabric_bastion_key
ssh-keygen -t rsa -b 3072 -N "" -f ~/.ssh/fabric_sliver_key
```

Register both `.pub` halves in the portal. On Windows, `chmod 600` does not
change what OpenSSH sees — it reads the ACL — so if `tunnel` reports
`UNPROTECTED PRIVATE KEY FILE`, fix it with
`icacls <key> /inheritance:r /grant:r "%USERDOMAIN%\%USERNAME%:R"`.

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
| state-1 | `state` | 4 | 10 GB | 100 GB | nats, timescaledb, migrate, pg-backup |
| | | **18** | **38 GB** | **250 GB** | |

state-1 would rather have 16 GB (plan §10), but **a project without the
`VM.NoLimitRAM` tag is capped at 10 GB per VM**, and the orchestrator refuses
the entire slice rather than trimming it — with HTTP 500 "Internal Server
Error" and the real reason only in the response's `X-Error` header. `check`
reads your project's tags and fails early naming the tag, so that refusal is
legible before anything is submitted. Ask a project lead for the tag if you
want the full sizing; cores and disk are governed by `VM.NoLimitCPU` and
`VM.NoLimitDisk` separately.

If your project quota is smaller still, trim `Config.nodes` — the tiers are
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
