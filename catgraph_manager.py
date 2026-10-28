#!/usr/bin/env python3
"""
CatGraph instance manager.

Interactive terminal menu that manages every container created from the
CatGraph image, whether it was made by this script, by Docker Compose, or
by hand. Each instance gets its own SQLite database in a host folder, so
data outlives containers.

Needs only Python 3.8+ and the Docker CLI.
Run:  python3 catgraph_manager.py
"""

import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
# Fill in IMAGE (or EXPECTED_IMAGE_ID) before the first run; see "Instance manager" in README.md.
IMAGE = ""                            # your image tag, e.g. svind/catgraph:latest or catgraph:latest
EXPECTED_IMAGE_ID = ""                # optional: short ID of a known-good build (from `docker images`)
DATA_ROOT = "~/Documents/categoryDB/catgraph-dbs"
DEFAULT_PORT = 8007                   # first host port tried for new instances (change if you like)
CONTAINER_PORT = 8000                 # port uvicorn listens on inside the container
BIND_HOST = "0.0.0.0"                 # host interface for new instances (127.0.0.1 = this machine only)
CONTAINER_PREFIX = "catgraph-"
DB_FILENAME = "catgraph.db"
DATA_MOUNT = "/data"                  # where the image expects its data folder
RESTART_POLICY = "unless-stopped"
MANAGED_LABEL = "catgraph.managed"
INSTANCE_LABEL = "catgraph.instance"
# ----------------------------------------------------------------------------

DATA_ROOT_PATH = Path(os.path.realpath(os.path.expanduser(DATA_ROOT)))
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
ALL_INTERFACES = {"", "0.0.0.0", "::"}

USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def ok(msg: str) -> None:
    print(paint("✔ ", "32") + msg)


def warn(msg: str) -> None:
    print(paint("! ", "33") + msg)


def err(msg: str) -> None:
    print(paint("✘ ", "31") + msg)


def tilde(p) -> str:
    s = str(p)
    home = str(Path.home())
    return "~" + s[len(home):] if s == home or s.startswith(home + os.sep) else s


# ----------------------------------------------------------------------------
# Docker helpers
# ----------------------------------------------------------------------------
class DockerError(Exception):
    pass


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["docker", *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise DockerError("The docker command was not found on PATH.")
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip() or f"docker {args[0]} failed"
        raise DockerError(msg)
    return proc


def bare_id(image_id: str) -> str:
    return image_id.split(":", 1)[-1]


def image_id(ref: str) -> Optional[str]:
    proc = docker("image", "inspect", ref, "--format", "{{.Id}}", check=False)
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def inspect_all_containers() -> List[dict]:
    ids = docker("ps", "-aq", "--no-trunc").stdout.split()
    if not ids:
        return []
    # check=False: a container removed between `ps` and `inspect` makes inspect
    # exit non-zero, but it still prints JSON for the ones that exist.
    proc = docker("inspect", *ids, check=False)
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        raise DockerError((proc.stderr or "could not read docker inspect output").strip())


# ----------------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------------
@dataclass
class Instance:
    name: str
    container: Optional[str] = None       # None = data folder with no container
    status: str = "no container"
    running: bool = False
    host_ip: Optional[str] = None
    port: Optional[int] = None
    data_dir: Optional[Path] = None       # host folder bind-mounted as the data dir
    db_path: Optional[Path] = None
    db_exists: Optional[bool] = None
    data_note: Optional[str] = None       # set when the data location is uncertain
    origin: str = "data only"
    stale_image: bool = False

    @property
    def url(self) -> Optional[str]:
        if self.port is None:
            return None
        host = "localhost" if self.host_ip in ALL_INTERFACES or self.host_ip == "127.0.0.1" else self.host_ip
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    @property
    def exposed(self) -> bool:
        return self.port is not None and self.host_ip in ALL_INTERFACES


@dataclass
class State:
    image_ids: set
    run_image: Optional[str]
    notes: List[str]
    raw: List[dict]
    instances: List[Instance]
    ignored: List[str] = field(default_factory=list)


def resolve_image() -> Tuple[set, Optional[str], List[str]]:
    ids, notes, run_image = set(), [], None
    tag_id = image_id(IMAGE) if IMAGE else None
    exp_id = image_id(EXPECTED_IMAGE_ID) if EXPECTED_IMAGE_ID else None

    if tag_id:
        ids.add(tag_id)
        run_image = IMAGE
        if EXPECTED_IMAGE_ID and not bare_id(tag_id).startswith(EXPECTED_IMAGE_ID):
            notes.append(
                f"{IMAGE} now points to {bare_id(tag_id)[:12]}, not the expected "
                f"{EXPECTED_IMAGE_ID}. New instances use the tag; containers from both builds are listed."
            )
    elif IMAGE:
        notes.append(f"Image {IMAGE} was not found locally.")

    if exp_id:
        ids.add(exp_id)
        if not run_image:
            run_image = exp_id
            notes.append(f"New instances will use image ID {EXPECTED_IMAGE_ID} instead.")
    elif EXPECTED_IMAGE_ID and not tag_id:
        notes.append(f"Image ID {EXPECTED_IMAGE_ID} was not found locally either.")

    if not run_image:
        notes.append("Spawning is disabled until the image exists (e.g. `docker compose build`).")
    return ids, run_image, notes


def image_name_variants() -> set:
    names = {IMAGE} if IMAGE else set()
    if IMAGE.endswith(":latest"):
        names.add(IMAGE[: -len(":latest")])
    if EXPECTED_IMAGE_ID:
        names.update({EXPECTED_IMAGE_ID, "sha256:" + EXPECTED_IMAGE_ID})
    return names


def is_ours(c: dict, ids: set) -> bool:
    if c.get("Image") in ids:
        return True
    return (c.get("Config") or {}).get("Image", "") in image_name_variants()


def port_binding(c: dict) -> Tuple[Optional[str], Optional[int]]:
    key = f"{CONTAINER_PORT}/tcp"
    live = ((c.get("NetworkSettings") or {}).get("Ports") or {}).get(key)
    conf = ((c.get("HostConfig") or {}).get("PortBindings") or {}).get(key)
    for bindings in (live, conf):
        for b in bindings or []:
            hp = str(b.get("HostPort") or "")
            if hp.isdigit():
                return b.get("HostIp") or "", int(hp)
    return None, None


def locate_data(c: dict) -> Tuple[Optional[Path], Optional[Path], Optional[str]]:
    """Return (host data dir, host db path, note when it can't be determined)."""
    env = {}
    for e in (c.get("Config") or {}).get("Env") or []:
        if "=" in e:
            k, v = e.split("=", 1)
            env[k] = v
    db_in = env.get("DB_PATH", f"{DATA_MOUNT}/{DB_FILENAME}")
    db_dir_in = posixpath.dirname(db_in)

    best = None
    for m in c.get("Mounts") or []:
        dest = (m.get("Destination") or "").rstrip("/") or "/"
        if db_dir_in == dest or db_dir_in.startswith(dest.rstrip("/") + "/"):
            if best is None or len(dest) > len(best["Destination"].rstrip("/")):
                best = m

    if best is None:
        return None, None, f"no mount for {db_dir_in}; data lives inside the container"
    kind = best.get("Type")
    if kind == "bind":
        src = Path(os.path.realpath(best.get("Source", "")))
        rel = posixpath.relpath(db_in, best["Destination"])
        return src, src / rel, None
    if kind == "volume":
        return None, None, f"Docker volume '{best.get('Name', '?')}' (inside Docker's storage)"
    return None, None, f"{kind or 'unknown'} mount at {best.get('Destination')}"


def to_instance(c: dict, ids: set) -> Instance:
    cname = (c.get("Name") or "").lstrip("/")
    labels = (c.get("Config") or {}).get("Labels") or {}
    if labels.get(INSTANCE_LABEL):
        name = labels[INSTANCE_LABEL]
    elif cname.startswith(CONTAINER_PREFIX) and len(cname) > len(CONTAINER_PREFIX):
        name = cname[len(CONTAINER_PREFIX):]
    else:
        name = cname

    st = c.get("State") or {}
    status = st.get("Status", "unknown")
    health = (st.get("Health") or {}).get("Status")
    if health and st.get("Running"):
        status = f"{status} ({health})"

    if labels.get(MANAGED_LABEL) == "true":
        origin = "script"
    elif labels.get("com.docker.compose.project"):
        origin = f"compose:{labels['com.docker.compose.project']}"
    else:
        origin = "manual"

    host_ip, port = port_binding(c)
    data_dir, db_path, note = locate_data(c)
    db_exists = None
    if db_path is not None:
        try:
            db_exists = db_path.exists()
        except OSError:
            db_exists = None

    return Instance(
        name=name, container=cname, status=status, running=bool(st.get("Running")),
        host_ip=host_ip, port=port, data_dir=data_dir, db_path=db_path,
        db_exists=db_exists, data_note=note, origin=origin,
        stale_image=c.get("Image") not in ids,
    )


def build_state() -> State:
    ids, run_image, notes = resolve_image()
    raw = inspect_all_containers()
    instances, ignored = [], []
    for c in raw:
        if is_ours(c, ids):
            instances.append(to_instance(c, ids))
        elif (c.get("Name") or "").lstrip("/").startswith(CONTAINER_PREFIX):
            ignored.append((c.get("Name") or "").lstrip("/"))
    instances.sort(key=lambda i: (i.name.lower(), i.container or ""))

    # Data folders with a database but no container, so they can be revived.
    referenced = {i.data_dir for i in instances if i.data_dir}
    if DATA_ROOT_PATH.is_dir():
        for d in sorted(DATA_ROOT_PATH.iterdir()):
            try:
                has_db = d.is_dir() and (d / DB_FILENAME).exists()
            except OSError:
                has_db = False
            if has_db and Path(os.path.realpath(d)) not in referenced:
                instances.append(Instance(
                    name=d.name, data_dir=Path(os.path.realpath(d)),
                    db_path=d / DB_FILENAME, db_exists=True,
                ))
    return State(ids, run_image, notes, raw, instances, ignored)


# ----------------------------------------------------------------------------
# Ports
# ----------------------------------------------------------------------------
def container_ports(raw: List[dict]) -> Dict[int, str]:
    """Host ports claimed by any container (running or stopped), any image."""
    used: Dict[int, str] = {}
    for c in raw:
        name = (c.get("Name") or "").lstrip("/")
        sources = [
            (c.get("HostConfig") or {}).get("PortBindings") or {},
            (c.get("NetworkSettings") or {}).get("Ports") or {},
        ]
        for src in sources:
            for bindings in src.values():
                for b in bindings or []:
                    hp = str(b.get("HostPort") or "")
                    if hp.isdigit():
                        used.setdefault(int(hp), name)
    return used


def host_port_free(port: int) -> bool:
    for host in (BIND_HOST, "0.0.0.0"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
            except OSError:
                return False
    return True


def next_free_port(state: State) -> Optional[int]:
    used = container_ports(state.raw)
    for p in range(DEFAULT_PORT, 65536):
        if p not in used and host_port_free(p):
            return p
    return None


# ----------------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------------
def ask(prompt: str) -> str:
    return input(prompt).strip()


def confirm(prompt: str) -> bool:
    return ask(f"{prompt} [y/N]: ").lower() in ("y", "yes")


def pick(state: State, verb: str, allowed) -> Optional[Instance]:
    choices = [(n, i) for n, i in enumerate(state.instances, 1) if allowed(i)]
    if not choices:
        print(f"Nothing to {verb} right now.")
        return None
    print("Eligible: " + ", ".join(f"{n}) {i.name}" for n, i in choices))
    ans = ask(f"Which instance to {verb}? (number or name, blank to cancel): ")
    if not ans:
        return None
    for n, i in choices:
        if ans in (str(n), i.name, i.container):
            return i
    err("Not one of the eligible choices.")
    return None


def sharers_of(state: State, inst: Instance) -> List[Instance]:
    if inst.data_dir is None:
        return []
    return [i for i in state.instances
            if i is not inst and i.container and i.data_dir == inst.data_dir]


# ----------------------------------------------------------------------------
# Display
# ----------------------------------------------------------------------------
def show(state: State) -> None:
    line = "=" * 72
    print("\n" + line)
    print(paint("CATGRAPH INSTANCE MANAGER".center(72), "1"))
    print(line)
    print(f"Image:          {IMAGE or '(tag not set)'}" + (f"  (expected ID {EXPECTED_IMAGE_ID})" if EXPECTED_IMAGE_ID else ""))
    print(f"Data directory: {tilde(DATA_ROOT_PATH)}")
    for n in state.notes:
        warn(n)
    print()

    if not state.instances:
        print("  No CatGraph containers or databases found yet.")
    else:
        print(f" {'#':>2}  {'NAME':<20} {'STATUS':<20} {'URL':<24} SOURCE")
        for n, i in enumerate(state.instances, 1):
            status = i.status
            color = "32" if i.running else ("2" if i.container is None else "33")
            print(f" {n:>2}  {i.name[:20]:<20} {paint(f'{status[:20]:<20}', color)} "
                  f"{(i.url or '-'):<24} {i.origin}")
            details = []
            if i.container:
                details.append(f"container {i.container}")
            if i.db_path is not None:
                db = tilde(i.db_path)
                if i.db_exists is False:
                    db += " (not created yet)"
                elif i.db_exists is None:
                    db += " (can't check)"
                details.append(f"db {db}")
            elif i.data_note:
                details.append(paint(f"data: {i.data_note}", "33"))
            print("      " + " · ".join(details))
            if i.exposed:
                print("      " + paint("exposed on all network interfaces, not just this machine", "33"))
            if i.stale_image:
                print("      " + paint("built from an older version of the image", "2"))
            if i.container is None:
                print("      " + paint("no container; spawn with this name to reuse the database", "2"))
    if state.ignored:
        print(paint(f"\n  Not listed (other images): {', '.join(state.ignored)}", "2"))

    print("-" * 72)
    print(" 1. Spawn a new instance")
    print(" 2. Start an existing instance")
    print(" 3. Stop an instance")
    print(" 4. Delete a container (keeps its data)")
    print(" 5. Delete an instance and its data")
    print(" 6. Refresh / list all instances")
    print(" 7. Exit")
    print("-" * 72)


# ----------------------------------------------------------------------------
# Operations
# ----------------------------------------------------------------------------
def spawn(state: State) -> None:
    if not state.run_image:
        err("No CatGraph image available to spawn from. Build it first.")
        return
    name = ask("Instance name (letters, digits, - _ .; blank to cancel): ")
    if not name:
        return
    if not NAME_RE.match(name):
        err("Use letters, digits, '-', '_' or '.', starting with a letter or digit (max 63).")
        return
    cname = CONTAINER_PREFIX + name
    existing = {(c.get("Name") or "").lstrip("/") for c in state.raw}
    if cname in existing:
        err(f"A container named {cname} already exists. Start it, or delete it first.")
        return

    data_dir = DATA_ROOT_PATH / name
    probe = Instance(name=name, data_dir=Path(os.path.realpath(data_dir)))
    sharing = sharers_of(state, probe)
    if sharing:
        warn("This data folder is already mounted by: " + ", ".join(i.container for i in sharing))
        warn("Two servers writing the same SQLite file at once can corrupt it.")
        if not confirm("Create another container on the same data anyway?"):
            return

    db = data_dir / DB_FILENAME
    if db.exists():
        ok(f"Found an existing database at {tilde(db)}; it will be reused.")
    elif data_dir.exists() and any(data_dir.iterdir()):
        warn(f"{tilde(data_dir)} exists but has no {DB_FILENAME}; a new database will be created there.")
    else:
        print(f"A new database will be created at {tilde(db)}.")

    used = container_ports(state.raw)
    suggested = next_free_port(state)
    raw_port = ask(f"Host port [{suggested if suggested else 'none free'}]: ")
    if raw_port:
        if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
            err("Port must be a number from 1 to 65535.")
            return
        port = int(raw_port)
        if port in used:
            err(f"Port {port} is already assigned to container {used[port]}.")
            return
        if not host_port_free(port):
            err(f"Port {port} is already in use by another program on this machine.")
            return
    elif suggested is None:
        err("No free port found.")
        return
    else:
        port = suggested

    data_dir.mkdir(parents=True, exist_ok=True)  # create it as you, not as root
    docker(
        "run", "-d",
        "--name", cname,
        "--restart", RESTART_POLICY,
        "-p", f"{BIND_HOST}:{port}:{CONTAINER_PORT}",
        "-v", f"{data_dir}:{DATA_MOUNT}",
        "--label", f"{MANAGED_LABEL}=true",
        "--label", f"{INSTANCE_LABEL}={name}",
        state.run_image,
    )
    host = "localhost" if BIND_HOST in ("127.0.0.1", "0.0.0.0", "") else BIND_HOST
    ok(f"Started {cname} at http://{host}:{port} (data: {tilde(data_dir)})")


def start(state: State) -> None:
    inst = pick(state, "start", lambda i: i.container and not i.running)
    if not inst:
        return
    running_sharers = [i for i in sharers_of(state, inst) if i.running]
    if running_sharers:
        warn("Already running on the same data folder: " + ", ".join(i.container for i in running_sharers))
        if not confirm("Start anyway? (two servers on one SQLite file risks corruption)"):
            return
    docker("start", inst.container)
    ok(f"Started {inst.container}" + (f" at {inst.url}" if inst.url else ""))


def stop(state: State) -> None:
    inst = pick(state, "stop", lambda i: i.container and i.running)
    if not inst:
        return
    print(f"Stopping {inst.container}...")
    docker("stop", inst.container)
    ok(f"Stopped {inst.container}. Its data is untouched.")


def remove_container(inst: Instance) -> None:
    if inst.running:
        print(f"Stopping {inst.container}...")
        docker("stop", inst.container)
    # No -v flag: Docker volumes, named or anonymous, are kept.
    docker("rm", inst.container)


def delete_container(state: State) -> None:
    inst = pick(state, "delete (container only)", lambda i: i.container is not None)
    if not inst:
        return
    if inst.data_dir is None and inst.data_note and inst.data_note.startswith("no mount"):
        warn(f"{inst.container} has no data mount, so its database lives inside the container.")
        warn("Deleting the container destroys that data permanently.")
        if ask(f"Type '{inst.container}' to confirm: ") != inst.container:
            print("Cancelled.")
            return
    else:
        where = tilde(inst.data_dir) if inst.data_dir else inst.data_note
        prompt = f"{'Stop and remove' if inst.running else 'Remove'} container {inst.container}? Data stays at {where}."
        if not confirm(prompt):
            return
    remove_container(inst)
    ok(f"Removed {inst.container}.")
    if inst.data_dir and inst.data_dir.parent == DATA_ROOT_PATH:
        print(f"  To reconnect later, spawn an instance named '{inst.data_dir.name}'.")


def inside_data_root(p: Path) -> bool:
    try:
        rel = Path(os.path.realpath(p)).relative_to(DATA_ROOT_PATH)
    except ValueError:
        return False
    return len(rel.parts) >= 1


def delete_folder(path: Path, state: State) -> None:
    try:
        shutil.rmtree(path)
        return
    except FileNotFoundError:
        return
    except PermissionError:
        pass
    # Files written by the container may be owned by root on Linux.
    # Remove them from inside a throwaway container instead of asking for sudo.
    if not state.run_image:
        raise DockerError(f"Permission denied deleting {path}; remove it manually (sudo rm -rf).")
    print("Some files are owned by root; removing them through Docker...")
    docker("run", "--rm", "--entrypoint", "rm",
           "-v", f"{path.parent}:/target",
           state.run_image, "-rf", f"/target/{path.name}")


def delete_with_data(state: State) -> None:
    inst = pick(state, "delete together with its data", lambda i: True)
    if not inst:
        return
    if inst.data_dir is None:
        err(f"Its data isn't in a host folder this script manages ({inst.data_note}).")
        print("  Use option 4 to remove the container, and delete the data yourself if you mean to.")
        return
    if not inside_data_root(inst.data_dir):
        err(f"Its data folder {tilde(inst.data_dir)} is outside {tilde(DATA_ROOT_PATH)}.")
        print("  For safety this script only deletes folders under the data directory.")
        print("  Use option 4 for the container and remove that folder manually if you're sure.")
        return
    sharing = sharers_of(state, inst)
    if sharing:
        err("Other containers use the same folder: " + ", ".join(i.container for i in sharing))
        print("  Delete those containers first.")
        return

    print(paint("This permanently deletes:", "31"))
    if inst.container:
        print(f"  container  {inst.container}")
    print(f"  folder     {tilde(inst.data_dir)} (including {DB_FILENAME})")
    if ask(f"Type the instance name '{inst.name}' to confirm: ") != inst.name:
        print("Cancelled.")
        return
    if inst.container:
        remove_container(inst)
        ok(f"Removed container {inst.container}.")
    delete_folder(inst.data_dir, state)
    ok(f"Deleted {tilde(inst.data_dir)}.")


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
def check_docker() -> bool:
    try:
        docker("info", "--format", "{{.ServerVersion}}")
        return True
    except DockerError as e:
        err("Can't talk to Docker: " + str(e).splitlines()[-1])
        if "permission denied" in str(e).lower():
            print("  Add yourself to the docker group (sudo usermod -aG docker $USER, then log in again),")
            print("  or run this script with sudo.")
        else:
            print("  Make sure Docker is installed and the daemon is running.")
        return False


ACTIONS = {"1": spawn, "2": start, "3": stop, "4": delete_container, "5": delete_with_data}


def main() -> int:
    if not IMAGE and not EXPECTED_IMAGE_ID:
        err("No image configured.")
        print("  Set IMAGE (and optionally EXPECTED_IMAGE_ID) at the top of catgraph_manager.py,")
        print("  e.g. IMAGE = \"svind/catgraph:latest\". See \"Instance manager\" in README.md.")
        return 1
    if not check_docker():
        return 1
    while True:
        try:
            state = build_state()
        except DockerError as e:
            err(str(e))
            return 1
        show(state)
        try:
            choice = ask("Choose an option: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice == "7" or choice.lower() in ("q", "quit", "exit"):
            print("Bye. Containers keep running as they are.")
            return 0
        if choice == "6" or choice == "":
            continue
        action = ACTIONS.get(choice)
        if not action:
            err("Choose a number from 1 to 7.")
            continue
        try:
            action(state)
        except DockerError as e:
            err(str(e))
        except KeyboardInterrupt:
            print("\nCancelled.")
        except EOFError:
            print()
            return 0


if __name__ == "__main__":
    sys.exit(main())
