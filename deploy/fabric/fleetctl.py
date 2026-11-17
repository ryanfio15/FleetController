#!/usr/bin/env python3
"""Provision FleetController on FABRIC, end to end, from this folder.

    python deploy/fabric/fleetctl.py check      validate credentials; create nothing
    python deploy/fabric/fleetctl.py up         slice -> RKE2 -> deps -> app
    python deploy/fabric/fleetctl.py keys        mint/renew the bastion key
    python deploy/fabric/fleetctl.py status      where everything stands
    python deploy/fabric/fleetctl.py kubeconfig  (re-)fetch the kubeconfig
    python deploy/fabric/fleetctl.py tunnel      the SSH tunnels, for a browser
    python deploy/fabric/fleetctl.py renew       extend the slice lease
    python deploy/fabric/fleetctl.py reset       tear RKE2 down, keep the slice
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
            # 10GB, not the 16 the plan asks for: a project without the
            # VM.NoLimitRAM tag is capped at 10GB per VM, and the orchestrator
            # rejects the whole slice rather than trimming it. Disk and cores
            # are unaffected -- cloud-edu carries VM.NoLimitDisk and
            # VM.NoLimitCPU, and it is disk that §6.4 sizes state-1 for.
            Node("state-1", "state", 4, 10, 100),
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


def find_bash() -> str:
    """Absolute path to a POSIX bash. Never the bare name "bash".

    CreateProcess resolves a bare executable name against System32 *before*
    PATH, and Windows ships C:\\Windows\\System32\\bash.exe -- the WSL
    launcher. So subprocess.run(["bash", ...]) does not run the Git Bash on
    PATH; it runs a Linux bash in a different filesystem namespace, with its
    own PATH and no idea what C:\\Users means. The symptoms are all
    misdirection:

      /bin/bash: C:UsersRyan...fc.sh: No such file or directory
      ./.env: line 3: $'\\r': command not found
      fc.sh: line 161: kubectl: command not found

    and check_local passes throughout, because shutil.which walks PATH and
    truthfully reports Git Bash. Resolving the path here makes the check and
    the call agree.
    """
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found

    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if Path(candidate).is_file():
            return candidate

    die(
        "no POSIX bash found (the System32 one is WSL and will not work)",
        "Install Git for Windows, or put its bin directory on PATH ahead of"
        "\nC:\\Windows\\System32. Checked:\n"
        f"  shutil.which('bash') -> {found or '(nothing)'}",
    )


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

    if require_kubectl:
        if not shutil.which("kubectl"):
            die("kubectl not found on PATH")
        find_bash()  # dies with a specific message if only WSL's bash exists

    ok("credentials and tooling present")


def ensure_home_env() -> None:
    """Give fablib a HOME, which PowerShell and cmd do not set.

    fablib computes DEFAULT_WORK_DIR = f"{os.environ['HOME']}/work" in the body
    of its Constants class, so a missing HOME is a KeyError raised during
    `import` -- before any fablib call, and with a traceback that points at
    constants.py rather than at the shell. Git Bash sets HOME and Windows sets
    USERPROFILE, which is why this breaks in PowerShell only.

    USERPROFILE is used rather than Path.home() so the work directory resolves
    to the same place in every shell: C:/Users/<you>/work/fabric_config, which
    is where fablib keeps its generated keys and its own ssh_config.
    """
    if os.environ.get("HOME"):
        return
    home = os.environ.get("USERPROFILE") or str(Path.home())
    os.environ["HOME"] = home
    info(f"HOME was unset; using {home} (fablib reads it at import time)")


def patch_windows_tempfile() -> None:
    """Work around an fim bug that only bites on Windows.

    fim serialises every topology it receives -- the advertised resources and
    each slice -- to a NamedTemporaryFile, then re-opens it *by name* to parse
    it (fim/graph/networkx_property_graph.py). With `delete=True`, its default,
    Windows opens the file with FILE_FLAG_DELETE_ON_CLOSE and a share mode that
    refuses the second open, so nx.read_graphml raises PermissionError. POSIX
    allows it, which is why this is invisible on Linux and in JupyterHub.

    fim swallows that with a bare `except: pass` and reports only
    "Unable to read graph <tmp path>", naming a file that no longer exists by
    the time you look. Hence patching here rather than guessing from the
    message: `check` otherwise dies at the site lookup, and `--site` only moves
    the failure to `submit`, which parses the slice topology the same way.

    Python 3.12 has delete_on_close for exactly this, so the file is still
    cleaned up at context exit. On 3.11 it has to be unlinked at exit instead.
    """
    if sys.platform != "win32":
        return

    import atexit
    import tempfile

    original = tempfile.NamedTemporaryFile
    if sys.version_info >= (3, 12):

        def patched(*args, **kwargs):
            kwargs.setdefault("delete_on_close", False)
            return original(*args, **kwargs)

    else:
        pending: list[str] = []

        def patched(*args, **kwargs):
            if kwargs.get("delete", True):
                kwargs["delete"] = False
                handle = original(*args, **kwargs)
                pending.append(handle.name)
                return handle
            return original(*args, **kwargs)

        @atexit.register
        def _cleanup() -> None:
            for name in pending:
                try:
                    os.unlink(name)
                except OSError:
                    pass

    tempfile.NamedTemporaryFile = patched  # type: ignore[assignment]


def import_fablib():
    ensure_home_env()
    patch_windows_tempfile()
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


# A project without VM.NoLimitRAM may not provision a VM with more than this
# much memory. The orchestrator enforces it at submission and fails the entire
# slice with HTTP 500 "Internal Server Error", putting the real reason only in
# the X-Error header -- so it is worth checking here instead.
PER_VM_RAM_CAP_GB = 10
NO_RAM_LIMIT_TAGS = ("VM.NoLimitRAM", "VM.NoLimit")


def check_project_limits(fablib, cfg: Config) -> None:
    """Fail before submission if a node exceeds the project's per-VM RAM cap.

    Policy is evaluated by FABRIC's PDP only once the whole slice is submitted,
    and a violation reads as an internal server error. Catching it here costs
    one API call and names the tag to ask for.
    """
    oversized = [n for n in cfg.nodes if n.ram > PER_VM_RAM_CAP_GB]
    if not oversized:
        return

    try:
        projects = fablib.get_manager().get_project_info(
            project_id=os.environ["FABRIC_PROJECT_ID"]
        )
        tags = set(projects[0].get("tags") or []) if projects else set()
    except Exception as exc:  # noqa: BLE001 - fablib raises bare Exceptions
        # Not worth blocking a submission over: the orchestrator is still the
        # authority, and this check is only here to make its refusal legible.
        warn(f"could not read project tags ({exc}); skipping the RAM check")
        return

    if any(tag in tags for tag in NO_RAM_LIMIT_TAGS):
        return

    names = ", ".join(f"{n.name} ({n.ram}GB)" for n in oversized)
    die(
        f"project '{projects[0].get('name')}' caps VMs at {PER_VM_RAM_CAP_GB}GB RAM",
        f"Over the cap: {names}\n\n"
        f"Project tags: {', '.join(sorted(tags)) or '(none)'}\n\n"
        f"Either ask a project lead for the {NO_RAM_LIMIT_TAGS[0]} tag, or lower\n"
        f"the ram= values in Config.nodes. The tiers are what the Kubernetes\n"
        f"overlay pins to, not the sizes, so trimming them changes nothing\n"
        f"about where workloads land.",
    )


def find_slice(fablib, name: str):
    """Return the named slice, or None if it does not exist.

    fablib's own get_slice *raises* when nothing matches rather than returning
    None, so every caller below would otherwise need its own try/except -- and
    "no slice yet" is the normal state for the first `up`, not an error. The
    message is matched rather than the exception type because fablib raises
    bare Exceptions, and swallowing all of them here would hide an expired
    token as "no slice".
    """
    try:
        return fablib.get_slice(name=name)
    except Exception as exc:  # noqa: BLE001 - fablib raises bare Exceptions
        if "unable to find slice" in str(exc).lower():
            return None
        raise


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
    existing = find_slice(fablib, cfg.slice_name)
    if existing is not None:
        state = existing.get_state()
        ok(f"slice '{cfg.slice_name}' already exists ({state})")
        if state not in ("StableOK", "ModifyOK"):
            warn(f"state is {state}; if this is wrong, run `destroy` and retry")
        return existing

    check_project_limits(fablib, cfg)
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


# RKE2 picks its cluster and service CIDRs from the node's primary address at
# first start. On FABRIC a VM's primary address is its IPv6 management address,
# so an unconfigured install comes up IPv6-only: the kubernetes Service lands on
# fd00:43::1 and canal's install-cni dies with an i/o timeout reaching it, which
# leaves every agent NotReady and reads as a CNI problem rather than an
# addressing one.
#
# Everything this project does is IPv4 -- FABNetv4 addresses, the NodePort the
# sims dial, the ipBlock in the NetworkPolicy, the IPs in the ingest
# certificate. So the families are pinned rather than detected. These are RKE2's
# own IPv4 defaults, stated explicitly; neither overlaps FABNetv4's 10.128/10.
CLUSTER_CIDR = "10.42.0.0/16"
SERVICE_CIDR = "10.43.0.0/16"
CLUSTER_DNS = "10.43.0.10"


def rke2_config(tier: str, extra: str = "", *, server: bool = False,
                node_ip: str | None = None) -> str:
    """The /etc/rancher/rke2/config.yaml body for one node.

    node-label is the load-bearing part: deploy/k8s/overlays/fabric/placement.yaml
    pins every workload with nodeSelector tier=..., so a node that comes up
    without its label leaves pods Pending with no other symptom.

    Assembled line by line rather than from an indented f-string. The previous
    version interpolated `extra` into an indented template and then called
    textwrap.dedent -- but dedent strips the *common* leading whitespace, and
    `extra`'s second and later lines bring their own indentation (zero, for
    `token:`). So dedent measured the wrong prefix: the template lines stayed
    indented and the injected ones did not, producing a file that is not valid
    YAML at all.

    RKE2 then read it leniently instead of refusing it, which is what made this
    expensive. The server kept its node-label and came up healthy while
    silently dropping tls-san; the agents lost both `server` and `token` and
    died with "Error: --token is required" on a file that visibly contained a
    token. Building the text explicitly removes the whole class.

    write-kubeconfig-mode is server-only: an agent warns "Unknown flag ...
    skipping", and a warning that is always present is a warning nobody reads.
    """
    lines: list[str] = []
    if server:
        lines.append('write-kubeconfig-mode: "0644"')
    # node-ip pins the kubelet to the FABNetv4 address. Without it the node
    # registers under its IPv6 management address, and the API server then
    # cannot reach the kubelet on :10250 -- `kubectl logs` fails with a 502
    # from the supervisor proxy.
    if node_ip:
        lines.append(f'node-ip: "{node_ip}"')
    lines += ["node-label:", f'  - "tier={tier}"']
    lines += [line.rstrip() for line in extra.strip().splitlines() if line.strip()]
    return "\n".join(lines) + "\n"


def configure_node_routes(node, addr: str, gateway: str) -> None:
    """Give the node IPv4 routes that Kubernetes assumes already exist.

    FABRIC VMs route externally over IPv6 only: the IPv4 table holds the
    management /23, the FABNetv4 /24 and nothing else -- no default route. The
    kernel therefore has no route covering the service CIDR, so a connection to
    a ClusterIP fails at connect() with ENETUNREACH *before* kube-proxy's DNAT
    rules are consulted. The rules are installed and correct; the packet never
    reaches them.

    That is what stops the cluster bootstrapping: canal's install-cni has to
    POST to the kubernetes Service to mint its own kubeconfig token, and it
    dies with "network is unreachable" on every node, so the CNI never lands
    and every node stays NotReady.

    A route for the service CIDR is enough. The lookup then succeeds, DNAT
    rewrites the destination to a real endpoint, and the packet is rerouted to
    an address that is genuinely on-link -- nothing ever ARPs for a ClusterIP.
    The pod CIDR is left to canal, which installs more specific routes itself.

    The FABNet route is re-applied here too: fablib's ip_route_add reported no
    error but left no route behind, and the nodes need it to reach anything on
    the testbed outside their own subnet.
    """
    iface = remote(
        node,
        f"ip -4 -o addr show | awk -v a='{addr}/' '$4 ~ a {{print $2; exit}}'",
        check=False,
    ).strip()
    if not iface:
        warn(f"[{node.get_name()}] no interface carries {addr}; skipping routes")
        return

    remote(node, f"sudo ip route replace {SERVICE_CIDR} dev {iface}", check=False)
    remote(node, f"sudo ip route replace {FABNET_V4} via {gateway} dev {iface}",
           check=False)
    # Calico gives every pod `default via 169.254.1.1` and answers the ARP for
    # it with proxy_arp on the host side of the veth. The kernel only proxies
    # ARP for an address it has a route to, and on a normal node the default
    # route covers it. There is no IPv4 default route here, so without this the
    # pod cannot resolve its own gateway and reaches nothing at all -- which
    # looks like broken policy rather than broken routing, because the CNI,
    # felix and the veths all report healthy.
    remote(node, f"sudo ip route replace 169.254.0.0/16 dev {iface} scope link",
           check=False)
    check = remote(node, f"ip route get 10.43.0.1 2>&1 | head -1", check=False)
    if "unreachable" in check.lower():
        die(
            f"{node.get_name()} still cannot route the service CIDR",
            f"interface={iface}\n{check}",
        )
    ok(f"{node.get_name()}: {iface} routes {SERVICE_CIDR} and {FABNET_V4}")


def fabnet_iface(node, addr: str) -> str:
    """Name of the interface carrying this node's FABNetv4 address."""
    return remote(
        node,
        f"ip -4 -o addr show | awk -v a='{addr}/' '$4 ~ a {{print $2; exit}}'",
        check=False,
    ).strip()


def configure_cni(node, iface: str) -> None:
    """Pin canal to the FABNetv4 interface.

    Flannel chooses its interface by looking up the IPv4 *default route*, and
    FABRIC VMs have none -- they route externally over IPv6 only. So flannel
    exits with "failed to get default interface: unable to find default route"
    and canal crash-loops on every node, even though the CNI binaries are
    installed and the nodes report Ready. Calico's address autodetection has
    the same default and would pick the management interface rather than
    FABNetv4.

    Dropped into the server's manifests directory rather than applied with
    kubectl: RKE2 reconciles that directory, so the override survives a restart
    and is reapplied if the chart is upgraded.
    """
    body = textwrap.dedent(
        f"""\
        apiVersion: helm.cattle.io/v1
        kind: HelmChartConfig
        metadata:
          name: rke2-canal
          namespace: kube-system
        spec:
          valuesContent: |-
            flannel:
              iface: "{iface}"
            calico:
              ipAutoDetectionMethod: "interface={iface}"
        """
    )
    path = "/var/lib/rancher/rke2/server/manifests/rke2-canal-config.yaml"
    remote(node, f"sudo tee {path} >/dev/null <<'EOF'\n{body}EOF",
           f"pinning canal to {iface}")
    ok(f"wrote {path}")


def service_is_active(node, unit: str) -> bool:
    """True only when systemd reports exactly "active".

    `"active" in output` is wrong, and it fails in the most damaging direction:
    systemctl prints **inactive** for a unit that does not exist, and "inactive"
    contains "active" as a substring. So a brand-new node looked like one where
    RKE2 was already running, the installer was skipped on all five nodes, and
    the first thing to actually touch the cluster -- reading the join token --
    failed on a file that was never created.

    Compare the last line exactly. systemctl's vocabulary here is active /
    inactive / failed / activating / deactivating / unknown, and only the first
    means the service is up.
    """
    out = remote(node, f"systemctl is-active {unit} 2>/dev/null || true",
                 check=False)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return bool(lines) and lines[-1] == "active"


def write_rke2_config(node, body: str) -> None:
    """Write /etc/rancher/rke2/config.yaml, then read it back and compare.

    The heredoc is quoted ('EOF') so the remote shell performs no substitution,
    and the result is verified rather than assumed. A config that arrives
    mangled is precisely the failure this guards against: RKE2 reads a broken
    config.yaml leniently instead of refusing it, so the damage surfaces later
    as a missing node label or a missing token -- never as a parse error.
    """
    remote(node, "sudo mkdir -p /etc/rancher/rke2", "writing config")
    remote(node, f"sudo tee /etc/rancher/rke2/config.yaml >/dev/null <<'EOF'\n{body}EOF")
    back = remote(node, "sudo cat /etc/rancher/rke2/config.yaml", check=False)
    if back.strip() != body.strip():
        die(
            f"config.yaml on {node.get_name()} does not match what was sent",
            "Sent:\n" + textwrap.indent(body, "    ")
            + "\nGot back:\n" + textwrap.indent(back, "    "),
        )


def wait_for_apiserver(node, attempts: int = 60) -> None:
    info("waiting for the API server (this takes a couple of minutes)")
    for attempt in range(attempts):
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
            return
    die(
        "the RKE2 API server never became ready",
        f"Inspect it with:  journalctl -u rke2-server -n 100\n"
        f"on {node.get_name()} ({node.get_management_ip()})",
    )


def install_server(node, cfg: Config, control_addr: str) -> str:
    """Install the RKE2 server on cp-1 and return the cluster join token."""
    stage(f"installing RKE2 server on {node.get_name()}")

    # tls-san covers the addresses the API server will be reached on: the
    # FABNetv4 address, and 127.0.0.1 because the fetched kubeconfig is used
    # through an SSH tunnel.
    # ingress-controller: RKE2 v1.36 deploys traefik by default and writes
    # rke2-ingress-nginx.yaml as a "# disabled by configuration" stub. This
    # project's only Ingress asks for ingressClassName: nginx, and the fabric
    # overlay's HelmChartConfig reconfigures `rke2-ingress-nginx` onto NodePort
    # 30080 -- neither of which means anything under traefik. Asking for
    # ingress-nginx keeps kind and FABRIC on the same manifests, which is the
    # property the overlay exists to preserve. The first value listed also
    # becomes the default IngressClass.
    extra = textwrap.dedent(
        f"""\
        advertise-address: "{control_addr}"
        cluster-cidr: "{CLUSTER_CIDR}"
        service-cidr: "{SERVICE_CIDR}"
        cluster-dns: "{CLUSTER_DNS}"
        ingress-controller:
          - "ingress-nginx"
        tls-san:
          - "{control_addr}"
          - "127.0.0.1"
          - "localhost"
        """
    )
    desired = rke2_config(cfg.control.tier, extra, server=True,
                          node_ip=control_addr)

    if service_is_active(node, "rke2-server"):
        # Converge the config even when the service is up, instead of trusting
        # that a running server was configured correctly. A malformed config is
        # exactly the case that leaves rke2-server healthy and missing tls-san,
        # and "already running" would otherwise make that permanent.
        current = remote(node, "sudo cat /etc/rancher/rke2/config.yaml 2>/dev/null || true",
                         check=False)
        if current.strip() == desired.strip():
            ok("rke2-server already running, config matches")
        else:
            warn("rke2-server is running with a different config -- rewriting")
            write_rke2_config(node, desired)
            remote(node, "sudo systemctl restart rke2-server", "restarting",
                   check=False)
            wait_for_apiserver(node)
    else:
        write_rke2_config(node, desired)
        version = f"INSTALL_RKE2_VERSION={cfg.rke2_version} " if cfg.rke2_version else ""
        remote(node, f"curl -sfL https://get.rke2.io | sudo {version}sh -",
               "running the installer", check=False)
        remote(node, "sudo systemctl enable --now rke2-server", "starting",
               check=False)
        wait_for_apiserver(node)

    token = remote(node, "sudo cat /var/lib/rancher/rke2/server/node-token")
    if not token:
        die("could not read the cluster join token from cp-1")
    return token


def install_agent(node, tier: str, cfg: Config, control_addr: str, token: str,
                  node_ip: str | None = None) -> None:
    name = node.get_name()
    if service_is_active(node, "rke2-agent"):
        ok(f"{name}: rke2-agent already running")
        return

    info(f"[{name}] installing agent (tier={tier})")
    extra = f'server: "https://{control_addr}:9345"\ntoken: "{token}"\n'
    write_rke2_config(node, rke2_config(tier, extra, node_ip=node_ip))
    version = f"INSTALL_RKE2_VERSION={cfg.rke2_version} " if cfg.rke2_version else ""
    remote(node, f"curl -sfL https://get.rke2.io | sudo {version}"
                 f"INSTALL_RKE2_TYPE=agent sh -", check=False)
    # restart, not enable --now: a crash-looping unit from a previous run is
    # already enabled and "activating", so --now is a no-op and the new config
    # never gets picked up.
    remote(node, "sudo systemctl enable rke2-agent", check=False)
    remote(node, "sudo systemctl restart rke2-agent", check=False)
    state = remote(node, "sleep 5; systemctl is-active rke2-agent || true",
                   check=False)
    if "failed" in state or "inactive" in state:
        warn(f"{name}: rke2-agent is '{state}' -- "
             f"journalctl -u rke2-agent -n 50 on that node")
    else:
        ok(f"{name}: agent started ({state})")


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
        # as_posix() rather than str(): kubectl is native and accepts either,
        # but a backslash path reaches bash as escape sequences.
        "KUBECONFIG": KUBECONFIG.as_posix(),
        "K8S_OVERLAY": "fabric",
    }
    # "fc.sh", relative to cwd, and not an absolute path of any shape. A bash
    # spawned from Python inherits no MSYS environment, so it resolves "/c/..."
    # against its own install root and reads "C:/..." as a *relative* path
    # beginning with a directory named "C:". Both report
    # "No such file or directory" for a file that is plainly there. Relative to
    # cwd sidesteps the whole question.
    result = subprocess.run(
        [find_bash(), "fc.sh", "k8s-deps"], env=env, cwd=ROOT
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

    # The Ingress moved out of grafana.yaml and was renamed grafana -> fleet
    # when it grew the /api and / routes. kubectl prunes nothing, so on a
    # cluster that ran the older manifests both exist for an instant -- and
    # ingress-nginx's admission webhook rejects the *whole apply* with
    #   host "_" and path "/grafana" is already defined in ingress fleet/grafana
    # rather than merely tolerating the overlap. Deleting first is not tidying.
    kubectl(["-n", "fleet", "delete", "ingress", "grafana", "--ignore-not-found"])

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
    check_project_limits(fablib, cfg)
    site = pick_site(fablib, cfg)
    ok(f"site with capacity: {site}")

    existing = find_slice(fablib, cfg.slice_name)
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

    slice_ = (
        build_slice(fablib, cfg)
        if should("slice")
        else find_slice(fablib, cfg.slice_name)
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
        # Before RKE2, not after: the service-CIDR route is what lets canal
        # bootstrap at all, and a node without it joins and then sits NotReady.
        stage("IPv4 routes for the cluster networks")
        gateway = str(slice_.get_network(name=cfg.network_name).get_gateway())
        for node_cfg in cfg.nodes:
            configure_node_routes(
                by_name[node_cfg.name], addresses[node_cfg.name], gateway
            )

        token = install_server(by_name[cfg.control.name], cfg, control_addr)

        # Before the agents join: canal rolls out as a DaemonSet, and pinning
        # the interface up front saves every node a crash-loop.
        ifaces = {
            n.name: fabnet_iface(by_name[n.name], addresses[n.name])
            for n in cfg.nodes
        }
        distinct = set(ifaces.values())
        if len(distinct) != 1 or not distinct.pop():
            die(
                "nodes disagree about which interface carries FABNetv4",
                "\n".join(f"{k:9s} {v or '(none)'}" for k, v in ifaces.items())
                + "\n\ncanal takes a single interface name for the whole"
                "\ncluster, so these have to match.",
            )
        configure_cni(by_name[cfg.control.name], ifaces[cfg.control.name])

        stage("installing RKE2 agents")
        for node_cfg in cfg.agents:
            install_agent(
                by_name[node_cfg.name], node_cfg.tier, cfg, control_addr, token,
                node_ip=addresses[node_cfg.name],
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
        print(tunnel_command(
            cfg,
            by_name[cfg.control.name].get_management_ip(),
            by_name["edge-1"].get_management_ip(),
        ))
        print()
        wait_for_api()

    if should("deps"):
        install_deps()
    if should("app"):
        deploy_app(edge_addr, node_cidr)

    stage("done")
    info(f"Grafana, once the tunnel is open: http://localhost:30080/grafana")
    info(f"Verify:  KUBECONFIG={KUBECONFIG.relative_to(ROOT)} ./fc.sh k8s-verify")


def tunnel_command(cfg: Config, control_mgmt: str, edge_mgmt: str) -> str:
    """The SSH forwards, addressed by *management* IP.

    Not the FABNetv4 address. The bastion has no route to the dataplane
    network, so `ssh -J bastion ubuntu@10.130.129.2` hangs and eventually
    reports "Connection timed out during banner exchange" -- which reads like a
    bastion or key problem rather than a wrong destination. FABNetv4 is for
    traffic *between* nodes; the bastion reaches a VM only on the management
    address, which on FABRIC is IPv6.

    The forwards still say localhost, because they are resolved on the far side
    of the hop: 127.0.0.1:6443 is the RKE2 API server and 127.0.0.1:30080 is
    the ingress-nginx NodePort, both bound on every node's loopback.

    Written on one line each: the printed command gets pasted into whatever
    shell the operator has, and a trailing backslash is a line continuation in
    bash but not in PowerShell, where it silently truncates the command.
    """
    user = os.environ.get("FABRIC_BASTION_USERNAME", "<bastion-user>")
    key = os.environ.get("FABRIC_SLICE_PRIVATE_KEY_FILE", "<sliver-key>")
    bastion = os.environ.get("FABRIC_BASTION_HOST", "bastion.fabric-testbed.net")
    return textwrap.indent(
        textwrap.dedent(
            f"""\
            # kubectl (6443) and Grafana/ingress (30080), via cp-1:
            ssh -i {key} -J {user}@{bastion} -L 6443:localhost:6443 -L 30080:localhost:30080 ubuntu@{control_mgmt}

            # 30443 (ingest) lives on edge-1, so for device traffic from your
            # laptop use a second session:
            ssh -i {key} -J {user}@{bastion} -L 30443:localhost:30443 ubuntu@{edge_mgmt}"""
        ),
        "      ",
    )


def cmd_tunnel(cfg: Config, _args) -> None:
    check_local(require_kubectl=False)
    fablib = connect(cfg)
    slice_ = find_slice(fablib, cfg.slice_name)
    if slice_ is None:
        die(f"no slice named '{cfg.slice_name}'")
    addresses, _ = configure_network(slice_, cfg)
    stage("tunnels")
    by_name = {n.get_name(): n for n in slice_.get_nodes()}
    print(tunnel_command(
        cfg,
        by_name[cfg.control.name].get_management_ip(),
        by_name["edge-1"].get_management_ip(),
    ))


def harden_key_file(key: Path) -> None:
    """Make a key fablib just wrote actually usable by OpenSSH on Windows.

    Two separate problems, both silent:

    1. fablib writes keys in Python *text* mode, so on Windows every "\\n"
       becomes "\\r\\n". OpenSSH cannot parse the mangled base64 and fails with
       `Load key "...": error in libcrypto` -- but only at the moment it tries
       to *sign*. The public half parses fine, so `ssh -v` reports
       "Server accepts key" and then "Permission denied", which reads exactly
       like a key that was never registered. It is the most misleading failure
       in this whole flow, so it is fixed rather than documented.

    2. Windows' OpenSSH reads the ACL, not the mode bits, and refuses a private
       key other accounts can read. chmod does nothing here. Note the grant is
       F and not R: read-only would stop a later `keys` run from replacing an
       expired key, and OpenSSH only cares that *other* accounts have no
       access.
    """
    if not key.is_file():
        return

    raw = key.read_bytes()
    unixed = raw.replace(b"\r\n", b"\n")
    if unixed != raw:
        key.write_bytes(unixed)
        ok(f"{key.name}: converted CRLF to LF ({len(raw)} -> {len(unixed)} bytes)")

    if sys.platform != "win32" or key.suffix == ".pub":
        return

    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
    result = local(
        ["icacls", str(key), "/inheritance:r", "/grant:r", f"{user}:F"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        ok(f"{key.name}: restricted to {user}")
    else:
        warn(f"{key.name}: icacls failed -- {(result.stderr or '').strip()[:120]}")


def cmd_reset(cfg: Config, args) -> None:
    """Tear RKE2 down on every node, leaving the slice itself untouched.

    Needed when something fixed at cluster-creation time has to change. The
    cluster and service CIDRs are the case that forced this to exist: RKE2
    derives them from the node's primary address on first start, and on FABRIC
    that is the IPv6 management address, so the cluster comes up IPv6-only and
    canal cannot reach the kubernetes Service to install the CNI. Restarting
    with a corrected config does nothing, because the CIDRs are already in
    etcd -- the datastore has to go.

    This destroys the Kubernetes cluster and everything in it. It does not
    delete the slice, so the VMs, their addresses and the lease survive and the
    next `up --only rke2` rebuilds in minutes rather than re-provisioning.
    """
    check_local(require_kubectl=False)
    fablib = connect(cfg)
    slice_ = find_slice(fablib, cfg.slice_name)
    if slice_ is None:
        die(f"no slice named '{cfg.slice_name}'")

    if not args.yes:
        print(f"\n{_BOLD}About to destroy the Kubernetes cluster on all "
              f"{len(cfg.nodes)} nodes.{_OFF}")
        print("The slice and its VMs survive; etcd and every workload do not.")
        print("Type the slice name to confirm: ", end="")
        if input().strip() != cfg.slice_name:
            die("not confirmed; nothing changed")

    by_name = {n.get_name(): n for n in slice_.get_nodes()}
    for node_cfg in cfg.nodes:
        node = by_name.get(node_cfg.name)
        if node is None:
            continue
        stage(f"resetting {node_cfg.name}")
        # killall first: it stops the services and unwinds the network
        # namespaces and mounts that a plain `rm -rf` would leave behind.
        remote(node, "sudo /usr/local/bin/rke2-killall.sh >/dev/null 2>&1 || true",
               "rke2-killall", check=False)
        remote(node, "sudo systemctl disable --now rke2-server rke2-agent "
                     ">/dev/null 2>&1 || true", check=False)
        remote(node, "sudo rm -rf /var/lib/rancher/rke2 /etc/rancher/rke2 "
                     "/etc/rancher/node /var/lib/kubelet", check=False)
        left = remote(node, "ls /var/lib/rancher/rke2 2>/dev/null | wc -l",
                      check=False)
        ok(f"{node_cfg.name}: cleared" if left.strip() in ("0", "")
           else f"{node_cfg.name}: {left.strip()} entries remain under /var/lib/rancher/rke2")

    stage("done")
    info("rebuild with:  fleetctl.py up --only rke2,kubeconfig")


def cmd_keys(cfg: Config, _args) -> None:
    """Mint and register a bastion key, via fablib's own flow.

    You cannot register a bastion key you generated yourself: the Core API's
    create_ssh_keys call takes a key *type*, not a public key, so FABRIC always
    mints the pair and hands back the private half. The portal works the same
    way. Sliver keys are different -- fablib injects whatever
    FABRIC_SLICE_PUBLIC_KEY_FILE contains into each VM at slice creation, so a
    self-generated sliver key needs no registration at all.

    verify_and_configure leaves a valid bastion key alone, so this is safe to
    re-run; it is also what to run when the key expires.
    """
    check_local(require_kubectl=False)
    fablib = connect(cfg)

    stage("validating FABRIC configuration and bastion keys")
    info("a bastion key that is missing or expired is regenerated and")
    info(f"written to {os.environ['FABRIC_BASTION_KEY_LOCATION']}")
    fablib.verify_and_configure()

    # Written by Python, so it carries inherited ACLs. Windows' OpenSSH reads
    # the ACL rather than the mode bits, and refuses a key other accounts can
    # read -- chmod alone does not fix it.
    for var in (
        "FABRIC_BASTION_KEY_LOCATION",
        "FABRIC_SLICE_PRIVATE_KEY_FILE",
        "FABRIC_SLICE_PUBLIC_KEY_FILE",
    ):
        path = os.environ.get(var)
        if path:
            harden_key_file(Path(path))

    stage("confirm it works before creating a slice")
    info("`check` never opens an SSH connection, so it passes either way:")
    info("")
    info("    ssh -o BatchMode=yes -v bastion.fabric-testbed.net exit")
    info("")
    info("Look for 'Authentication succeeded (publickey)'. The session then")
    info("closes immediately -- the bastion grants no shell, which is normal.")


def cmd_status(cfg: Config, _args) -> None:
    check_local(require_kubectl=False)
    fablib = connect(cfg)
    slice_ = find_slice(fablib, cfg.slice_name)
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
    slice_ = find_slice(fablib, cfg.slice_name)
    if slice_ is None:
        die(f"no slice named '{cfg.slice_name}'")
    addresses, _ = configure_network(slice_, cfg)
    by_name = {n.get_name(): n for n in slice_.get_nodes()}
    fetch_kubeconfig(by_name[cfg.control.name], addresses[cfg.control.name])


def cmd_renew(cfg: Config, args) -> None:
    check_local(require_kubectl=False)
    fablib = connect(cfg)
    slice_ = find_slice(fablib, cfg.slice_name)
    if slice_ is None:
        die(f"no slice named '{cfg.slice_name}'")
    end = datetime.now(timezone.utc) + timedelta(days=args.days)
    stage(f"renewing until {end:%Y-%m-%d %H:%M} UTC")
    slice_.renew(end.strftime("%Y-%m-%d %H:%M:%S %z") or end.isoformat())
    ok("renewed")


def cmd_destroy(cfg: Config, args) -> None:
    check_local(require_kubectl=False)
    fablib = connect(cfg)
    slice_ = find_slice(fablib, cfg.slice_name)
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
    "keys": cmd_keys,
    "status": cmd_status,
    "kubeconfig": cmd_kubeconfig,
    "tunnel": cmd_tunnel,
    "renew": cmd_renew,
    "reset": cmd_reset,
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
