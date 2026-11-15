#!/usr/bin/env python3
"""Provision FleetController on FABRIC, end to end, from this folder.

    python deploy/fabric/fleetctl.py check      validate credentials; create nothing
    python deploy/fabric/fleetctl.py up         slice -> RKE2 -> deps -> app
    python deploy/fabric/fleetctl.py status      where everything stands
    python deploy/fabric/fleetctl.py kubeconfig  (re-)fetch the kubeconfig
    python deploy/fabric/fleetctl.py tunnel      the SSH tunnels, for a browser
    python deploy/fabric/fleetctl.py renew       extend the slice lease
    python deploy/fabric/fleetctl.py destroy     delete the slice

`up` is the whole thing and is safe to re-run: it reuses an existing slice,
skips RKE2 where it is already installed, and re-applies the manifests. Each
stage can also be run on its own with --only, which is what you want when one
step fails and the rest are already done.

Why a script rather than a notebook: slice leases expire, so this gets run more
than once, and the two values the Kubernetes overlay needs — edge-1's address
and the FABNetv4 subnet — are *discovered* here rather than typed in. That
removes the main chance to deploy something that looks healthy and cannot
connect.

                      ---- read this before the first run ----

Nothing in this file has been run against the real testbed. It is written to
fablib's documented API, but FABRIC-side behaviour (site capacity, image names,
how long post-boot config takes) is not something the author could verify. Run
`check` first: it talks to FABRIC read-only, prints what it finds, and creates
nothing. If an API call in here is wrong, that is where it surfaces, for the
price of nothing.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # repository root
KUBECONFIG = HERE / "kubeconfig"  # gitignored

# FABNetv4's global address space. Nodes need an explicit route to it; without
# one they can reach their own subnet and nothing else on the testbed.
FABNET_V4 = "10.128.0.0/10"


# --------------------------------------------------------------------------- #
# Configuration
#
# Sizing matches docs/TECHNICAL_PLAN.md §10. state-1 is oversized on purpose: a
# 20Gi readings PVC plus 5Gi of JetStream plus WAL plus the pg-backup target,
# and §6.4 shows 20Gi is only about five days of headroom at 50 cars even with
# the retention policies in place.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Node:
    name: str
    tier: str  # the nodeSelector label deploy/k8s/overlays/fabric pins to
    cores: int
    ram: int  # GB
    disk: int  # GB


@dataclass
class Config:
    slice_name: str = "fleetcontroller"
    site: str | None = None  # None = let fablib pick one with capacity
    image: str = "default_ubuntu_22"
    network_name: str = "fabnet"
    rke2_version: str = ""  # "" = the installer's stable channel
    lease_days: int = 14
    nodes: list[Node] = field(
        default_factory=lambda: [
            Node("cp-1", "control", 4, 8, 50),
            Node("edge-1", "edge", 2, 4, 20),
            Node("worker-1", "pipeline", 4, 8, 40),
            Node("worker-2", "pipeline", 4, 8, 40),
            Node("state-1", "state", 4, 16, 100),
        ]
    )

    @property
    def control(self) -> Node:
        return self.nodes[0]

    @property
    def agents(self) -> list[Node]:
        return self.nodes[1:]

    def total(self) -> tuple[int, int, int]:
        return (
            sum(n.cores for n in self.nodes),
            sum(n.ram for n in self.nodes),
            sum(n.disk for n in self.nodes),
        )


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

_BOLD, _DIM, _RED, _GREEN, _YELLOW, _OFF = (
    ("\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m")
    if sys.stdout.isatty()
    else ("", "", "", "", "", "")
)


def stage(msg: str) -> None:
    print(f"\n{_BOLD}==> {msg}{_OFF}", flush=True)


def info(msg: str) -> None:
    print(f"    {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"    {_GREEN}ok{_OFF}   {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"    {_YELLOW}warn{_OFF} {msg}", flush=True)


def die(msg: str, hint: str = "") -> "NoReturn":  # type: ignore[valid-type]
    print(f"\n{_RED}error{_OFF} {msg}", file=sys.stderr)
    if hint:
        print(textwrap.indent(hint.strip(), "      "), file=sys.stderr)
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
# Local prerequisites
# --------------------------------------------------------------------------- #

# fablib reads these from ~/.fabric_rc or the environment. They are listed here
# so a missing one is reported by name up front, rather than as an opaque
# failure three minutes into a slice submission.
REQUIRED_VARS = [
    ("FABRIC_PROJECT_ID", "your project's GUID, from the FABRIC portal"),
    ("FABRIC_TOKEN_LOCATION", "path to the token JSON downloaded from the portal"),
    ("FABRIC_BASTION_USERNAME", "portal username, e.g. ryanfio15_0000012345"),
    ("FABRIC_BASTION_KEY_LOCATION", "private key registered as your bastion key"),
    ("FABRIC_SLICE_PRIVATE_KEY_FILE", "private half of your sliver keypair"),
    ("FABRIC_SLICE_PUBLIC_KEY_FILE", "public half of your sliver keypair"),
]


def load_fabric_rc() -> None:
    """Source ~/.fabric_rc into the environment if it has not been already.

    fablib picks this file up on its own in a Jupyter kernel, but a plain
    `python fleetctl.py` in a terminal does not, which is the single most
    common reason this works in a notebook and not here.
    """
    rc = Path(os.environ.get("FABRIC_RC", Path.home() / ".fabric_rc"))
    if not rc.is_file():
        return
    for line in rc.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    info(f"loaded {rc}")


def check_local(require_kubectl: bool = True) -> None:
    load_fabric_rc()

    missing = [(k, why) for k, why in REQUIRED_VARS if not os.environ.get(k)]
    if missing:
        die(
            f"{len(missing)} FABRIC setting(s) not set",
            "\n".join(f"{k:32s} {why}" for k, why in missing)
            + "\n\nPut them in ~/.fabric_rc (the portal generates one for you), or"
            "\nexport them in this shell. See deploy/fabric/README.md.",
        )

    for key in (
        "FABRIC_TOKEN_LOCATION",
        "FABRIC_BASTION_KEY_LOCATION",
        "FABRIC_SLICE_PRIVATE_KEY_FILE",
    ):
        path = Path(os.environ[key]).expanduser()
        if not path.is_file():
            die(f"{key} points at a file that does not exist: {path}")

    tools = ["kubectl", "bash"] if require_kubectl else []
    for tool in tools:
        if not shutil.which(tool):
            die(f"{tool} not found on PATH")

    ok("credentials and tooling present")


def import_fablib():
    try:
        from fabrictestbed_extensions.fablib.fablib import FablibManager
    except ImportError:
        die(
            "fabrictestbed-extensions is not installed",
            f"pip install -r {HERE.relative_to(ROOT)}/requirements.txt\n\n"
            f"Note: this machine is on Python {sys.version_info.major}."
            f"{sys.version_info.minor}. fablib has a narrower supported range;"
            " if the install fails, use a 3.11 or 3.12 virtualenv.",
        )
    return FablibManager


# --------------------------------------------------------------------------- #
# Shell helpers
# --------------------------------------------------------------------------- #


def local(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command on this machine, echoing it first."""
    info(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, **kw)


def remote(node, cmd: str, label: str = "", check: bool = True) -> str:
    """Run a command on a slice node over the bastion.

    fablib tunnels this for us, which is the whole reason a single script can
    provision the cluster without the operator opening an SSH session.
    """
    if label:
        info(f"[{node.get_name()}] {label}")
    stdout, stderr = node.execute(cmd, quiet=True)
    if check and stderr and stderr.strip():
        # Not every write to stderr is fatal -- apt and curl both chatter -- so
        # this is surfaced rather than raised on.
        tail = stderr.strip().splitlines()[-1][:160]
        warn(f"[{node.get_name()}] stderr: {tail}")
    return (stdout or "").strip()


# --------------------------------------------------------------------------- #
# Stage: slice
# --------------------------------------------------------------------------- #


def connect(cfg: Config):
    FablibManager = import_fablib()
    try:
        fablib = FablibManager()
    except Exception as exc:  # noqa: BLE001 - fablib raises bare Exceptions
        die(
            f"could not initialise fablib: {exc}",
            "A 401/403 here almost always means an expired token: download a"
            " fresh one from the FABRIC portal to FABRIC_TOKEN_LOCATION.",
        )
    return fablib


def pick_site(fablib, cfg: Config) -> str:
    if cfg.site:
        return cfg.site
    cores, ram, disk = cfg.total()
    info(f"looking for a site with {cores} cores / {ram}GB RAM / {disk}GB disk")
    site = fablib.get_random_site(
        filter_function=lambda s: (
            s["cores_available"] > cores
            and s["ram_available"] > ram
            and s["disk_available"] > disk
        )
    )
    if not site:
        die(
            "no FABRIC site currently advertises enough free capacity",
            "Check the portal's resource view, then pass --site explicitly or"
            " trim the sizing in Config.nodes.",
        )
    return site


def build_slice(fablib, cfg: Config):
    """Create the slice, or return the existing one untouched."""
    existing = fablib.get_slice(name=cfg.slice_name)
    if existing is not None:
        state = existing.get_state()
        ok(f"slice '{cfg.slice_name}' already exists ({state})")
        if state not in ("StableOK", "ModifyOK"):
            warn(f"state is {state}; if this is wrong, run `destroy` and retry")
        return existing

    site = pick_site(fablib, cfg)
    cores, ram, disk = cfg.total()
    stage(f"creating slice '{cfg.slice_name}' at {site}  ({cores}c/{ram}GB/{disk}GB)")

    slice_ = fablib.new_slice(name=cfg.slice_name)
    # One L3 FABNetv4 network, so the nodes get routable addresses and the sims
    # can reach edge-1's NodePort the way a real device would (§10).
    net = slice_.add_l3network(name=cfg.network_name, type="IPv4")

    for node_cfg in cfg.nodes:
        node = slice_.add_node(
            name=node_cfg.name,
            site=site,
            cores=node_cfg.cores,
            ram=node_cfg.ram,
            disk=node_cfg.disk,
            image=cfg.image,
        )
        iface = node.add_component(
            model="NIC_Basic", name=f"{node_cfg.name}-nic"
        ).get_interfaces()[0]
        net.add_interface(iface)
        info(f"{node_cfg.name:9s} tier={node_cfg.tier:9s} "
             f"{node_cfg.cores}c/{node_cfg.ram}GB/{node_cfg.disk}GB")

    info("submitting -- this usually takes several minutes")
    slice_.submit(wait=True, progress=True)
    ok("slice is up")
    return slice_


def configure_network(slice_, cfg: Config) -> tuple[dict[str, str], str]:
    """Give every node a FABNetv4 address and a route to the rest of FABNet.

    Returns {node name: address} and the subnet in CIDR form. Both are what the
    Kubernetes overlay's placeholders get filled with, which is why they are
    read back from the slice rather than configured from a constant.
    """
    stage("configuring FABNetv4 addresses")
    net = slice_.get_network(name=cfg.network_name)
    subnet = net.get_subnet()
    gateway = net.get_gateway()
    available = net.get_available_ips()
    info(f"subnet {subnet}  gateway {gateway}")

    addresses: dict[str, str] = {}
    for node in slice_.get_nodes():
        name = node.get_name()
        iface = node.get_interface(network_name=cfg.network_name)
        addr = available.pop(0)
        try:
            iface.ip_addr_add(addr=addr, subnet=subnet)
            node.ip_route_add(subnet=FABNET_V4, gateway=gateway)
        except Exception as exc:  # noqa: BLE001
            # Re-running `up` hits this: the address is already configured.
            warn(f"[{name}] address/route step: {exc}")
        addresses[name] = str(addr)
        info(f"{name:9s} {addr}")

    return addresses, str(subnet)


# --------------------------------------------------------------------------- #
# Stage: RKE2
# --------------------------------------------------------------------------- #


def rke2_config(tier: str, extra: str = "") -> str:
    """The /etc/rancher/rke2/config.yaml body for one node.

    node-label is the load-bearing part: deploy/k8s/overlays/fabric/placement.yaml
    pins every workload with nodeSelector tier=..., so a node that comes up
    without its label leaves pods Pending with no other symptom.
    """
    return textwrap.dedent(
        f"""\
        write-kubeconfig-mode: "0644"
        node-label:
          - "tier={tier}"
        {extra}"""
    )


def install_server(node, cfg: Config, control_addr: str) -> str:
    """Install the RKE2 server on cp-1 and return the cluster join token."""
    stage(f"installing RKE2 server on {node.get_name()}")

    if "active" in remote(node, "systemctl is-active rke2-server || true", check=False):
        ok("rke2-server already running")
    else:
        # tls-san covers the addresses the API server will be reached on: the
        # FABNetv4 address, and 127.0.0.1 because the fetched kubeconfig is
        # used through an SSH tunnel.
        extra = textwrap.dedent(
            f"""\
            tls-san:
              - "{control_addr}"
              - "127.0.0.1"
              - "localhost"
            """
        )
        remote(node, "sudo mkdir -p /etc/rancher/rke2", "writing config")
        remote(
            node,
            f"sudo tee /etc/rancher/rke2/config.yaml >/dev/null <<'EOF'\n"
            f"{rke2_config(cfg.control.tier, extra)}\nEOF",
        )
        version = f"INSTALL_RKE2_VERSION={cfg.rke2_version} " if cfg.rke2_version else ""
        remote(node, f"curl -sfL https://get.rke2.io | sudo {version}sh -",
               "running the installer", check=False)
        remote(node, "sudo systemctl enable --now rke2-server", "starting",
               check=False)

        info("waiting for the API server (this takes a couple of minutes)")
        for attempt in range(60):
            time.sleep(10)
            probe = remote(
                node,
                "sudo /var/lib/rancher/rke2/bin/kubectl "
                "--kubeconfig /etc/rancher/rke2/rke2.yaml get --raw /readyz "
                "2>/dev/null || true",
                check=False,
            )
            if "ok" in probe:
                ok(f"API server ready after {(attempt + 1) * 10}s")
                break
        else:
            die(
                "the RKE2 API server never became ready",
                f"Inspect it with:  journalctl -u rke2-server -n 100\n"
                f"on {node.get_name()} ({node.get_management_ip()})",
            )

    token = remote(node, "sudo cat /var/lib/rancher/rke2/server/node-token")
    if not token:
        die("could not read the cluster join token from cp-1")
    return token


def install_agent(node, tier: str, cfg: Config, control_addr: str, token: str) -> None:
    name = node.get_name()
    if "active" in remote(node, "systemctl is-active rke2-agent || true", check=False):
        ok(f"{name}: rke2-agent already running")
        return

    info(f"[{name}] installing agent (tier={tier})")
    extra = f'server: "https://{control_addr}:9345"\ntoken: "{token}"\n'
    remote(node, "sudo mkdir -p /etc/rancher/rke2")
    remote(
        node,
        f"sudo tee /etc/rancher/rke2/config.yaml >/dev/null <<'EOF'\n"
        f"{rke2_config(tier, extra)}\nEOF",
    )
    version = f"INSTALL_RKE2_VERSION={cfg.rke2_version} " if cfg.rke2_version else ""
    remote(node, f"curl -sfL https://get.rke2.io | sudo {version}"
                 f"INSTALL_RKE2_TYPE=agent sh -", check=False)
    remote(node, "sudo systemctl enable --now rke2-agent", check=False)
    ok(f"{name}: agent started")


def fetch_kubeconfig(node, control_addr: str) -> None:
    """Pull cp-1's kubeconfig and point it at a local tunnel.

    RKE2 writes the file with server: https://127.0.0.1:6443, which is exactly
    what an SSH tunnel wants -- so this is kept as-is and `tunnel` provides the
    other end.
    """
    stage("fetching kubeconfig")
    raw = remote(node, "sudo cat /etc/rancher/rke2/rke2.yaml")
    if "clusters:" not in raw:
        die("cp-1 did not return a usable kubeconfig")
    KUBECONFIG.write_text(raw, encoding="utf-8")
    try:
        KUBECONFIG.chmod(0o600)
    except OSError:
        pass  # Windows
    ok(f"wrote {KUBECONFIG.relative_to(ROOT)} (gitignored)")
    info(f"it points at 127.0.0.1:6443 -- open the tunnel below, or use "
         f"`fleetctl.py tunnel`")
    info(f"cluster API is also at {control_addr}:6443 inside FABRIC")


# --------------------------------------------------------------------------- #
# Stage: the application
# --------------------------------------------------------------------------- #


def kubectl(args: list[str], check: bool = True, quiet: bool = False,
            stdin: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "KUBECONFIG": str(KUBECONFIG)}
    cmd = ["kubectl", *args]
    if not quiet:
        info(f"$ kubectl {' '.join(args)}")
    return subprocess.run(
        cmd, env=env, check=check, text=True, input=stdin,
        capture_output=quiet,
    )


def wait_for_api() -> None:
    info("waiting for the API server through the tunnel")
    for _ in range(30):
        result = kubectl(["version", "-o", "json"], check=False, quiet=True)
        if result.returncode == 0:
            ok("API reachable")
            return
        time.sleep(4)
    die(
        "cannot reach the cluster API",
        "The kubeconfig points at 127.0.0.1:6443, so an SSH tunnel has to be"
        " open. Start one with:\n\n"
        "    python deploy/fabric/fleetctl.py tunnel\n\n"
        "and leave it running in another terminal, then re-run with"
        " --only deps,app",
    )


def install_deps() -> None:
    """cert-manager, the CSI driver and KEDA.

    Delegates to ./fc.sh k8s-deps so the pinned versions live in exactly one
    place. RKE2 already ships metrics-server, local-path and ingress-nginx, and
    fc.sh skips those when the overlay is `fabric`.
    """
    stage("installing cluster dependencies")
    env = {
        **os.environ,
        "KUBECONFIG": str(KUBECONFIG),
        "K8S_OVERLAY": "fabric",
    }
    result = subprocess.run(
        ["bash", str(ROOT / "fc.sh"), "k8s-deps"], env=env, cwd=ROOT
    )
    if result.returncode != 0:
        die("./fc.sh k8s-deps failed -- see the output above")
    ok("cert-manager, csi-driver and KEDA installed")


def ensure_db_secret() -> None:
    """Create the fleet-db Secret with a generated password, once.

    Created imperatively rather than sealed, because this script already holds
    cluster access and a generated password never has to touch disk. If you
    want the manifests reproducible from the repository alone, seal it instead
    and uncomment the entry in the overlay -- see the overlay's README.
    """
    existing = kubectl(
        ["-n", "fleet", "get", "secret", "fleet-db", "-o", "name"],
        check=False, quiet=True,
    )
    if existing.returncode == 0:
        ok("fleet-db secret already present, leaving it alone")
        return

    kubectl(["create", "namespace", "fleet"], check=False, quiet=True)
    password = secrets.token_urlsafe(24)
    kubectl(
        [
            "-n", "fleet", "create", "secret", "generic", "fleet-db",
            "--from-literal=POSTGRES_USER=fleet",
            f"--from-literal=POSTGRES_PASSWORD={password}",
            "--from-literal=POSTGRES_DB=fleet",
        ],
        quiet=True,
    )
    ok("generated the fleet-db secret (password not written to disk)")


def render_overlay(edge_addr: str, node_cidr: str) -> str:
    """Render the fabric overlay with the discovered site values substituted.

    The placeholders are replaced here rather than edited into site.yaml so the
    committed file stays a template, and -- more importantly -- so the two
    values come from the live slice. Typing edge-1's address by hand is how you
    get a cluster that passes every health check and refuses every handshake.
    """
    overlay = ROOT / "deploy" / "k8s" / "overlays" / "fabric"
    result = subprocess.run(
        [
            "kubectl", "kustomize",
            "--load-restrictor", "LoadRestrictionsNone",
            str(overlay),
        ],
        cwd=ROOT, text=True, capture_output=True,
    )
    if result.returncode != 0:
        die(f"rendering the overlay failed:\n{result.stderr}")

    rendered = result.stdout
    for placeholder, value in (("EDGE1_ADDR", edge_addr), ("NODE_CIDR", node_cidr)):
        if placeholder not in rendered:
            warn(f"{placeholder} not found in the rendered overlay -- "
                 f"has site.yaml been edited by hand?")
        rendered = rendered.replace(placeholder, value)
    return rendered


def deploy_app(edge_addr: str, node_cidr: str) -> None:
    stage("deploying FleetController")
    info(f"edge-1 = {edge_addr}   FABNetv4 subnet = {node_cidr}")

    # kube-system, so it cannot be part of the namespaced overlay.
    kubectl([
        "apply", "-f",
        str(ROOT / "deploy/k8s/overlays/fabric/ingress-nginx-nodeport.yaml"),
    ])

    ensure_db_secret()

    # A Job's spec is immutable; adding a migration moves the generated
    # ConfigMap's name and the apply would fail with "field is immutable".
    kubectl(["-n", "fleet", "delete", "job", "migrate", "--ignore-not-found"])

    manifests = render_overlay(edge_addr, node_cidr)
    kubectl(["apply", "-f", "-"], stdin=manifests)
    ok("manifests applied")

    info("give it a minute, then:")
    info("  KUBECONFIG=deploy/fabric/kubeconfig ./fc.sh k8s-status")


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #


def node_tiers(cfg: Config) -> dict[str, str]:
    return {n.name: n.tier for n in cfg.nodes}


def cmd_check(cfg: Config, _args) -> None:
    stage("local prerequisites")
    check_local()

    stage("talking to FABRIC (read-only)")
    fablib = connect(cfg)
    fablib.show_config()

    cores, ram, disk = cfg.total()
    info(f"this slice would request {cores} cores / {ram}GB RAM / {disk}GB disk")
    site = pick_site(fablib, cfg)
    ok(f"site with capacity: {site}")

    existing = fablib.get_slice(name=cfg.slice_name)
    if existing is None:
        info(f"no slice named '{cfg.slice_name}' yet -- `up` would create it")
    else:
        ok(f"slice '{cfg.slice_name}' exists, state {existing.get_state()}")

    stage("nothing was created")
    info("if that all looks right:  python deploy/fabric/fleetctl.py up")


def cmd_up(cfg: Config, args) -> None:
    only = set(args.only.split(",")) if args.only else None

    def should(name: str) -> bool:
        return only is None or name in only

    check_local()
    fablib = connect(cfg)

    slice_ = build_slice(fablib, cfg) if should("slice") else fablib.get_slice(
        name=cfg.slice_name
    )
    if slice_ is None:
        die(f"no slice named '{cfg.slice_name}'", "Run without --only to create it.")

    if should("slice"):
        stage("waiting for SSH on every node")
        slice_.wait_ssh(progress=True)
        slice_.post_boot_config()
        ok("all nodes reachable")

    addresses, node_cidr = configure_network(slice_, cfg)
    control_addr = addresses[cfg.control.name]
    edge_addr = addresses["edge-1"]

    by_name = {n.get_name(): n for n in slice_.get_nodes()}
    tiers = node_tiers(cfg)

    if should("rke2"):
        token = install_server(by_name[cfg.control.name], cfg, control_addr)
        stage("installing RKE2 agents")
        for node_cfg in cfg.agents:
            install_agent(
                by_name[node_cfg.name], node_cfg.tier, cfg, control_addr, token
            )

        info("waiting for all five nodes to register")
        cp = by_name[cfg.control.name]
        for _ in range(45):
            time.sleep(10)
            out = remote(
                cp,
                "sudo /var/lib/rancher/rke2/bin/kubectl --kubeconfig "
                "/etc/rancher/rke2/rke2.yaml get nodes --no-headers 2>/dev/null "
                "| wc -l",
                check=False,
            )
            if out.strip().isdigit() and int(out.strip()) >= len(cfg.nodes):
                ok(f"{out.strip()} nodes registered")
                break
        else:
            warn("not all nodes registered yet; carrying on -- check `status` later")

        print()
        info("node labels, as the overlay's nodeSelectors will see them:")
        print(remote(cp,
                     "sudo /var/lib/rancher/rke2/bin/kubectl --kubeconfig "
                     "/etc/rancher/rke2/rke2.yaml get nodes -L tier", check=False))

    if should("kubeconfig") or should("rke2"):
        fetch_kubeconfig(by_name[cfg.control.name], control_addr)

    if should("deps") or should("app"):
        print()
        warn("the steps below need the SSH tunnel open, in another terminal:")
        print(tunnel_command(cfg, control_addr, edge_addr))
        print()
        wait_for_api()

    if should("deps"):
        install_deps()
    if should("app"):
        deploy_app(edge_addr, node_cidr)

    stage("done")
    info(f"Grafana, once the tunnel is open: http://localhost:30080/grafana")
    info(f"Verify:  KUBECONFIG={KUBECONFIG.relative_to(ROOT)} ./fc.sh k8s-verify")


def tunnel_command(cfg: Config, control_addr: str, edge_addr: str) -> str:
    user = os.environ.get("FABRIC_BASTION_USERNAME", "<bastion-user>")
    key = os.environ.get("FABRIC_SLICE_PRIVATE_KEY_FILE", "<sliver-key>")
    bastion = os.environ.get("FABRIC_BASTION_HOST", "bastion.fabric-testbed.net")
    return textwrap.indent(
        textwrap.dedent(
            f"""\
            ssh -i {key} \\
                -J {user}@{bastion} \\
                -L 6443:localhost:6443 \\
                -L 30080:localhost:30080 \\
                ubuntu@{control_addr}

            # 30443 (ingest) lives on edge-1, so for device traffic from your
            # laptop use a second session:
            ssh -i {key} -J {user}@{bastion} \\
                -L 30443:localhost:30443 ubuntu@{edge_addr}"""
        ),
        "      ",
    )


def cmd_tunnel(cfg: Config, _args) -> None:
    check_local(require_kubectl=False)
    fablib = connect(cfg)
    slice_ = fablib.get_slice(name=cfg.slice_name)
    if slice_ is None:
        die(f"no slice named '{cfg.slice_name}'")
    addresses, _ = configure_network(slice_, cfg)
    stage("tunnels")
    print(tunnel_command(cfg, addresses[cfg.control.name], addresses["edge-1"]))


def cmd_status(cfg: Config, _args) -> None:
    check_local(require_kubectl=False)
    fablib = connect(cfg)
    slice_ = fablib.get_slice(name=cfg.slice_name)
    if slice_ is None:
        info(f"no slice named '{cfg.slice_name}'")
        return

    stage(f"slice '{cfg.slice_name}'  state={slice_.get_state()}")
    try:
        info(f"lease ends {slice_.get_lease_end()}")
    except Exception:  # noqa: BLE001
        pass
    print(slice_.list_nodes())

    if KUBECONFIG.is_file():
        stage("cluster (needs the tunnel open)")
        result = kubectl(["get", "nodes", "-L", "tier"], check=False, quiet=True)
        if result.returncode == 0:
            print(result.stdout)
            kubectl(["-n", "fleet", "get", "pods", "-o", "wide"], check=False)
        else:
            info("API not reachable -- is the tunnel up?")


def cmd_kubeconfig(cfg: Config, _args) -> None:
    check_local(require_kubectl=False)
    fablib = connect(cfg)
    slice_ = fablib.get_slice(name=cfg.slice_name)
    if slice_ is None:
        die(f"no slice named '{cfg.slice_name}'")
    addresses, _ = configure_network(slice_, cfg)
    by_name = {n.get_name(): n for n in slice_.get_nodes()}
    fetch_kubeconfig(by_name[cfg.control.name], addresses[cfg.control.name])


def cmd_renew(cfg: Config, args) -> None:
    check_local(require_kubectl=False)
    fablib = connect(cfg)
    slice_ = fablib.get_slice(name=cfg.slice_name)
    if slice_ is None:
        die(f"no slice named '{cfg.slice_name}'")
    end = datetime.now(timezone.utc) + timedelta(days=args.days)
    stage(f"renewing until {end:%Y-%m-%d %H:%M} UTC")
    slice_.renew(end.strftime("%Y-%m-%d %H:%M:%S %z") or end.isoformat())
    ok("renewed")


def cmd_destroy(cfg: Config, args) -> None:
    check_local(require_kubectl=False)
    fablib = connect(cfg)
    slice_ = fablib.get_slice(name=cfg.slice_name)
    if slice_ is None:
        info(f"no slice named '{cfg.slice_name}' -- nothing to delete")
        return

    # Deleting a slice destroys the VMs and their local-path volumes, and the
    # data tier is single-node by design (§10). Anything not in a pg-backup
    # dump is gone.
    if not args.yes:
        print(f"\n{_BOLD}About to delete slice '{cfg.slice_name}' "
              f"({slice_.get_state()}).{_OFF}")
        print("All five VMs and every local-path volume go with it -- the")
        print("database included. Type the slice name to confirm: ", end="")
        if input().strip() != cfg.slice_name:
            die("not confirmed; nothing deleted")

    slice_.delete()
    ok("slice deleted")
    if KUBECONFIG.is_file():
        KUBECONFIG.unlink()
        info(f"removed {KUBECONFIG.relative_to(ROOT)}")


# --------------------------------------------------------------------------- #


COMMANDS = {
    "check": cmd_check,
    "up": cmd_up,
    "status": cmd_status,
    "kubeconfig": cmd_kubeconfig,
    "tunnel": cmd_tunnel,
    "renew": cmd_renew,
    "destroy": cmd_destroy,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fleetctl.py",
        description="Provision FleetController on FABRIC.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Run `check` first. It is read-only and creates nothing.

            Stages of `up`, usable with --only:
              slice       create the 5 VMs and the FABNetv4 network
              rke2        install the server and four agents, set tier labels
              kubeconfig  fetch cp-1's kubeconfig to deploy/fabric/kubeconfig
              deps        cert-manager, cert-manager-csi-driver, KEDA
              app         render the fabric overlay and apply it

            e.g.  fleetctl.py up --only deps,app      (tunnel already open)
            """
        ),
    )
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--slice-name", default=None, help="default: fleetcontroller")
    parser.add_argument("--site", default=None, help="FABRIC site, e.g. STAR")
    parser.add_argument("--image", default=None, help="default: default_ubuntu_22")
    parser.add_argument("--rke2-version", default=None, help="default: stable channel")
    parser.add_argument("--only", default=None, help="comma-separated stages of `up`")
    parser.add_argument("--days", type=int, default=14, help="renew: lease length")
    parser.add_argument("--yes", action="store_true", help="destroy: skip confirmation")
    args = parser.parse_args()

    cfg = Config()
    if args.slice_name:
        cfg.slice_name = args.slice_name
    if args.site:
        cfg.site = args.site
    if args.image:
        cfg.image = args.image
    if args.rke2_version:
        cfg.rke2_version = args.rke2_version

    try:
        COMMANDS[args.command](cfg, args)
    except KeyboardInterrupt:
        die("interrupted", "The slice may be half-built; `status` will say.")


if __name__ == "__main__":
    main()
