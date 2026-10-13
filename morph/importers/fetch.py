"""Import from a GitHub or Hugging Face link, with an allowlist and an SSRF guard.

  info = fetch("https://github.com/TheRobotStudio/SO-ARM100/tree/main/Simulation/SO100", "work/files")
  info = fetch("https://raw.githubusercontent.com/TheRobotStudio/SO-ARM100/main/Simulation/SO100/so100.urdf", ...)
  info = fetch("https://huggingface.co/datasets/<org>/<repo>/tree/main/robot", ...)

Rules (contracts section 9): HTTPS only; hosts github.com, codeload.github.com, raw.githubusercontent.com,
huggingface.co and *.hf.co; every hop's address must be public (no private, loopback, link-local, multicast or
reserved IPs), at most 3 redirects, each re-checked; 300 MB and 400 files at most; 30 s socket timeout. A direct
connection goes to the vetted IP (no DNS rebinding between check and connect); through an HTTPS proxy the
hostname must still be allowlisted. Off in remote/team mode unless the Studio was started with
--allow-url-import (the caller passes allow=False), and off when Morph is offline (MORPH_OFFLINE=1).

Only robot-relevant files are fetched from a Hugging Face tree; a GitHub link downloads the repository zip
(or, for a raw URDF/MJCF link, that file plus the meshes it references). Nothing fetched is ever executed.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import posixpath
import re
import socket
import ssl
import time
import urllib.request
from urllib.parse import quote, unquote, urljoin, urlsplit

from .common import LIMITS, MB, ImportFailed, contained, parse_xml, safe_relpath

ALLOW = {"github.com", "codeload.github.com", "raw.githubusercontent.com", "huggingface.co"}
ALLOW_SUFFIX = (".hf.co",)
WANTED = re.compile(r"\.(urdf|xacro|xml|stl|obj|ply|off|dae|glb|gltf|bin|3mf|step|stp|iges|igs|csv|tsv|xlsx|md|yaml|yml|"
                    r"kicad_pcb|kicad_sch)$|(^|/)(package\.xml|licen[sc]e[^/]*|copying[^/]*|notice[^/]*|readme[^/]*)$", re.I)
UA = "MorphStudio-import/0.2 (+https://github.com/jim4226/A16z_Hackathon)"
OVERALL_S = 600.0
SEG = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}$")      # an owner, repo or ref path segment; never . or ..


def _local(name: str) -> str:
    """A folder name for a downloaded repository (".github" -> "github"; never hidden, never a path)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).lstrip(".")[:100] or "repo"


def _usable(rel: str) -> bool:
    """A listed or referenced path we may write: relative, no '..' and nothing hidden (or reserved on Windows)."""
    try:
        safe_relpath(rel)
        return True
    except ImportFailed:
        return False


def network_allowed() -> bool:
    if os.environ.get("MORPH_OFFLINE", "").lower() in ("1", "true", "yes"):
        return False
    try:
        from .. import settings          # W0a: non-secret preferences (network on/off)
        return bool(settings.network_allowed())
    except Exception:  # noqa: BLE001  (settings not available: default on)
        return True


def allowed_host(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    return h in ALLOW or any(h.endswith(s) and len(h) > len(s) for s in ALLOW_SUFFIX)


def check_url(url: str):
    """Parsed URL if it may be fetched; ImportFailed with the reason otherwise."""
    if not isinstance(url, str) or len(url) > 2048:
        raise ImportFailed("Paste a GitHub or Hugging Face link (https://...).")
    u = urlsplit(url.strip())
    if u.scheme != "https":
        raise ImportFailed("Only https:// links can be imported.")
    if u.username or u.password or "@" in u.netloc:
        raise ImportFailed("Links with a user name or password are not imported.")
    host = (u.hostname or "").lower()
    if u.port not in (None, 443):
        raise ImportFailed("Links on a non-standard port are not imported.")
    try:
        ipaddress.ip_address(host)
        raise ImportFailed("Links to an IP address are not imported; use a github.com or huggingface.co link.")
    except ValueError:
        pass
    if not allowed_host(host):
        raise ImportFailed(f"{host or 'That host'} is not on the import list. Morph imports from github.com, "
                           "raw.githubusercontent.com and huggingface.co; download other files and drop them here.")
    return u


def _public(ip: str) -> bool:
    a = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved
                                or a.is_unspecified)


def resolve_public(host: str, port: int = 443) -> list:
    """All addresses of host; ImportFailed if any is not public (a DNS answer pointing inside the network)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return []
    ips = sorted({i[4][0] for i in infos})
    bad = [ip for ip in ips if not _public(ip)]
    if bad:
        raise ImportFailed(f"{host} resolves to a private or local address; Morph refuses to fetch it.")
    return ips


def _proxy(host: str):
    """(proxy host, port, auth header or None) for an https request, honouring HTTPS_PROXY / NO_PROXY."""
    p = urllib.request.getproxies().get("https")
    if not p or urllib.request.proxy_bypass(host):
        return None
    pu = urlsplit(p if "://" in p else "http://" + p)
    auth = None
    if pu.username:
        auth = "Basic " + base64.b64encode(f"{unquote(pu.username)}:{unquote(pu.password or '')}".encode()).decode()
    return pu.hostname, pu.port or 8080, auth


class _Pinned(http.client.HTTPSConnection):
    """HTTPS to a vetted IP with SNI and certificate checks for the hostname."""

    def __init__(self, host, ip, **kw):
        super().__init__(host, 443, **kw)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _connect(host: str, timeout: float):
    ctx = ssl.create_default_context()
    proxy = _proxy(host)
    ips = resolve_public(host)
    if proxy:
        conn = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=timeout, context=ctx)
        conn.set_tunnel(host, 443, headers={"Proxy-Authorization": proxy[2]} if proxy[2] else None)
        return conn
    if not ips:
        raise ImportFailed(f"Couldn't look up {host}; check the internet connection.")
    return _Pinned(host, ips[0], timeout=timeout, context=ctx)


def open_url(url: str, timeout: float | None = None, max_redirects: int | None = None, method: str = "GET"):
    """(final url, response) after at most 3 redirects, each hop re-validated. The caller reads and closes."""
    timeout = timeout or LIMITS["fetch_timeout"]
    hops = LIMITS["fetch_redirects"] if max_redirects is None else max_redirects
    for _ in range(hops + 1):
        u = check_url(url)
        try:
            conn = _connect(u.hostname, timeout)
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            conn.request(method, path, headers={"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"})
            resp = conn.getresponse()
        except ImportFailed:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise ImportFailed(f"Couldn't reach {u.hostname} ({exc.__class__.__name__}); check the connection and the link.") from None
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location")
            resp.close()
            conn.close()
            if not loc:
                raise ImportFailed(f"{u.hostname} redirected without a location.")
            url = urljoin(url, loc)
            continue
        return url, resp
    raise ImportFailed(f"More than {hops} redirects; Morph stopped following the link.")


def download(url: str, dest: str, max_bytes: int, budget: dict | None = None, deadline: float | None = None) -> int:
    """Stream url to dest (counting real bytes against max_bytes and the shared budget)."""
    final, resp = open_url(url)
    try:
        if resp.status != 200:
            raise ImportFailed(f"{urlsplit(final).hostname} answered {resp.status} for {posixpath.basename(urlsplit(url).path) or url}"
                               + (": the repository may be private, or the path wrong." if resp.status in (401, 403, 404) else "."))
        n = resp.getheader("Content-Length")
        if n and n.isdigit() and int(n) > max_bytes:
            raise ImportFailed(f"{posixpath.basename(urlsplit(url).path)} is {int(n) / MB:.0f} MB; the import limit is {max_bytes // MB} MB.")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        got = 0
        with open(dest, "wb") as out:
            while True:
                chunk = resp.read(MB)
                if not chunk:
                    break
                got += len(chunk)
                if budget is not None:
                    budget["bytes"] = budget.get("bytes", 0) + len(chunk)
                if got > max_bytes or (budget is not None and budget["bytes"] > LIMITS["fetch_bytes"]):
                    raise ImportFailed(f"The download passed the {LIMITS['fetch_bytes'] // MB} MB import limit.")
                if deadline and time.time() > deadline:
                    raise ImportFailed("The download took too long; download the files yourself and drop them here.")
                out.write(chunk)
        return got
    except BaseException:
        if os.path.exists(dest):
            os.remove(dest)
        raise
    finally:
        resp.close()


def _json_get(url: str, deadline: float):
    final, resp = open_url(url)
    try:
        if resp.status != 200:
            raise ImportFailed(f"Hugging Face answered {resp.status}: the repository may be private, gated or misspelled.")
        data = resp.read(20 * MB + 1)
        if len(data) > 20 * MB:
            raise ImportFailed("The file listing is too large.")
        link = resp.getheader("Link") or ""
    finally:
        resp.close()
    try:
        return json.loads(data.decode("utf-8")), link
    except (UnicodeDecodeError, ValueError):
        raise ImportFailed("Hugging Face sent a listing Morph couldn't read.") from None


# --------------------------------------------------------------------------------- link parsing
def plan(url: str) -> dict:
    """What a link means: {"kind": github_zip|github_raw|hf_tree|hf_file, ...}."""
    u = check_url(url)
    host = u.hostname.lower()
    parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
    seg = SEG
    if host == "github.com":
        if len(parts) < 2 or not all(seg.match(p) for p in parts[:2]):
            raise ImportFailed("Paste a repository link such as https://github.com/<owner>/<repo>.")
        owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
        ref, sub = None, ""
        if len(parts) >= 4 and parts[2] in ("tree", "blob"):
            ref = parts[3]
            sub = "/".join(parts[4:-1] if parts[2] == "blob" else parts[4:])
        elif len(parts) >= 4 and parts[2] == "archive":
            ref = re.sub(r"\.(zip|tar\.gz)$", "", "/".join(parts[3:]))
        zip_url = (f"https://codeload.github.com/{quote(owner)}/{quote(repo)}/zip/{quote(ref, safe='/')}" if ref
                   else f"https://github.com/{quote(owner)}/{quote(repo)}/archive/HEAD.zip")
        return {"kind": "github_zip", "owner": owner, "repo": repo, "ref": ref or "HEAD", "sub": sub, "zip": zip_url,
                "source_url": f"https://github.com/{owner}/{repo}" + (f"/tree/{ref}/{sub}".rstrip("/") if ref else "")}
    if host == "codeload.github.com":
        if len(parts) < 4 or parts[2] != "zip" or not all(seg.match(x) for x in parts[:2]):
            raise ImportFailed("Paste the repository's github.com link instead.")
        return {"kind": "github_zip", "owner": parts[0], "repo": parts[1], "ref": "/".join(parts[3:]), "sub": "",
                "zip": url, "source_url": f"https://github.com/{parts[0]}/{parts[1]}"}
    if host == "raw.githubusercontent.com":
        if len(parts) < 4 or not all(seg.match(x) for x in parts[:3]) or not _usable("/".join(parts[3:])):
            raise ImportFailed("Paste a raw file link: https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>.")
        return {"kind": "github_raw", "owner": parts[0], "repo": parts[1], "ref": parts[2], "path": "/".join(parts[3:]),
                "base": f"https://raw.githubusercontent.com/{quote(parts[0])}/{quote(parts[1])}/{quote(parts[2])}/",
                "source_url": f"https://github.com/{parts[0]}/{parts[1]}/blob/{parts[2]}/{'/'.join(parts[3:])}"}
    if host == "huggingface.co":
        kind = "models"
        if parts and parts[0] in ("datasets", "spaces"):
            kind, parts = parts[0], parts[1:]
        if len(parts) < 2 or not all(seg.match(p) for p in parts[:2]):
            raise ImportFailed("Paste a Hugging Face repository link such as https://huggingface.co/<owner>/<repo>.")
        repo = f"{parts[0]}/{parts[1]}"
        rev, sub, single = "main", "", False
        if len(parts) >= 4 and parts[2] in ("tree", "blob", "resolve"):
            rev = parts[3]
            sub = "/".join(parts[4:])
            single = parts[2] in ("blob", "resolve")
        prefix = "" if kind == "models" else f"{kind}/"
        return {"kind": "hf_file" if single else "hf_tree", "repo_type": kind, "repo": repo, "rev": rev, "sub": sub,
                "prefix": prefix, "source_url": f"https://huggingface.co/{prefix}{repo}"}
    raise ImportFailed("Paste a github.com, raw.githubusercontent.com or huggingface.co link.")


# --------------------------------------------------------------------------------- fetchers
def _github_zip(p: dict, dest: str, on_progress, deadline) -> dict:
    from . import zipsafe

    tmp = os.path.join(dest, f".download-{os.getpid()}.zip")
    budget = {}
    on_progress and on_progress(f"Downloading {p['owner']}/{p['repo']} ({p['ref']})")
    download(p["zip"], tmp, LIMITS["fetch_bytes"], budget, deadline)
    root = os.path.join(dest, _local(p["repo"]))
    unpack = os.path.join(dest, f".unpack-{os.getpid()}")
    try:
        files, warns = zipsafe.extract_zip(tmp, unpack)
    finally:
        os.remove(tmp)
    tops = {f.split("/", 1)[0] for f in files}
    base = os.path.join(unpack, tops.pop()) if len(tops) == 1 else unpack
    if p.get("sub"):
        base = contained(unpack, os.path.join(base, *p["sub"].split("/")))
        if not base or not os.path.isdir(base):
            import shutil
            shutil.rmtree(unpack, ignore_errors=True)
            raise ImportFailed(f"The folder {p['sub']} isn't in {p['owner']}/{p['repo']}.")
        root = os.path.join(dest, _local(os.path.basename(base.rstrip(os.sep)) or p["repo"]))
    os.replace(base, root)
    import shutil
    shutil.rmtree(unpack, ignore_errors=True)
    return _listing(dest, root)


def _listing(dest: str, root: str) -> dict:
    out = {}
    for dp, _, fns in os.walk(root):
        for f in fns:
            full = os.path.join(dp, f)
            out[os.path.relpath(full, dest).replace(os.sep, "/")] = os.path.getsize(full)
    return {"files": out, "bytes": sum(out.values())}


def _refs(path: str) -> list:
    """Relative files a URDF or MJCF references (meshes, includes), as (ref, kind)."""
    try:
        root = parse_xml(path)
    except ImportFailed:
        return []
    out = []
    if root.tag == "robot":
        out += [(m.get("filename"), "mesh") for m in root.iter("mesh") if m.get("filename")]
    elif root.tag == "mujoco":
        meshdir = next((c.get("meshdir") or c.get("assetdir") for c in root.iter("compiler")
                        if c.get("meshdir") or c.get("assetdir")), "")
        out += [(i.get("file"), "include") for i in root.iter("include") if i.get("file")]
        out += [(posixpath.join(meshdir, m.get("file")) if meshdir else m.get("file"), "mesh")
                for m in root.iter("mesh") if m.get("file")]
    return out


def _github_raw(p: dict, dest: str, on_progress, deadline) -> dict:
    root = os.path.join(dest, _local(p["repo"]))
    budget, done, todo = {}, {}, [[p["path"]]]
    n = 0
    while todo:
        alts = [a for a in todo.pop(0) if a not in done and _usable(a)]
        for rel in alts:
            n += 1
            if n > LIMITS["fetch_files"]:
                raise ImportFailed(f"The robot references more than {LIMITS['fetch_files']} files; use the repository link.")
            local = os.path.join(root, *rel.split("/"))
            if contained(root, local) is None:
                continue
            on_progress and on_progress(f"Fetching {rel}")
            try:
                done[rel] = download(p["base"] + quote(rel), local, LIMITS["zip_file"], budget, deadline)
            except ImportFailed:
                if rel == p["path"]:
                    raise
                continue                      # try the next guess; a missing mesh is reported by the importer
            if rel == p["path"] or rel.lower().endswith((".xml", ".urdf")):
                here = posixpath.dirname(rel)
                for ref, _ in _refs(local):
                    if ref.startswith("package://"):
                        pkg, _, rest = ref[len("package://"):].partition("/")
                        comps = here.split("/") if here else []
                        roots = ["/".join(comps[:i + 1]) for i, c in enumerate(comps) if c == pkg]
                        roots += [r for r in (here, posixpath.dirname(here)) if r not in roots]
                        todo.append([posixpath.normpath(posixpath.join(r, rest)) if r else rest for r in roots][:3])
                    elif not (ref.startswith(("file://", "/")) or "://" in ref):
                        todo.append([posixpath.normpath(posixpath.join(here, ref)) if here else posixpath.normpath(ref)])
            break
    for names in (("LICENSE", "LICENSE.md", "LICENSE.txt"), ("README.md",)):   # licence + motor hints, best effort
        for rel in names:
            try:
                download(p["base"] + rel, os.path.join(root, rel), 2 * MB, budget, deadline)
                break
            except ImportFailed:
                continue
    return _listing(dest, root)


def _hf(p: dict, dest: str, on_progress, deadline) -> dict:
    root = os.path.join(dest, _local(p["repo"].split("/")[1]))
    api = f"https://huggingface.co/api/{p['repo_type']}/{p['repo']}/tree/{quote(p['rev'], safe='')}"
    sub = p["sub"]
    if p["kind"] == "hf_file":
        sub = posixpath.dirname(sub)
    url = api + (f"/{quote(sub)}" if sub else "") + "?recursive=true"
    entries, pages = [], 0
    while url and pages < 6:
        data, link = _json_get(url, deadline)
        if not isinstance(data, list):
            raise ImportFailed("Hugging Face sent an unexpected listing.")
        entries += [e for e in data if isinstance(e, dict) and e.get("type") == "file"]
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m else None
        pages += 1
    files = [e for e in entries if isinstance(e.get("path"), str) and WANTED.search(e["path"])]
    gltf_dirs = {posixpath.dirname(e["path"]) for e in files if e["path"].lower().endswith(".gltf")}
    files = [e for e in files if not e["path"].lower().endswith(".bin") or posixpath.dirname(e["path"]) in gltf_dirs]
    if not files:
        raise ImportFailed("No robot, mesh or parts-list files in that Hugging Face folder.")
    if len(files) > LIMITS["fetch_files"]:
        raise ImportFailed(f"That folder has {len(files)} relevant files; link a smaller folder (at most {LIMITS['fetch_files']}).")
    size = sum(int((e.get("lfs") or {}).get("size") or e.get("size") or 0) for e in files)
    if size > LIMITS["fetch_bytes"]:
        raise ImportFailed(f"Those files add up to {size / MB:.0f} MB; the import limit is {LIMITS['fetch_bytes'] // MB} MB.")
    budget = {}
    for e in files:
        rel = e["path"][len(sub) + 1:] if sub and e["path"].startswith(sub + "/") else e["path"]
        local = os.path.join(root, *rel.split("/"))
        if not _usable(rel) or contained(root, local) is None:
            continue
        on_progress and on_progress(f"Fetching {rel}")
        download(f"https://huggingface.co/{p['prefix']}{p['repo']}/resolve/{quote(p['rev'], safe='')}/{quote(e['path'])}",
                 local, LIMITS["zip_file"], budget, deadline)
    return _listing(dest, root)


def fetch(url: str, dest: str, allow: bool = True, on_progress=None) -> dict:
    """Download what a link points to into dest/<repo>/...; returns {"files": {rel: size}, "bytes", "url", "source_url"}."""
    if not allow:
        raise ImportFailed("Importing from a link is off on this shared Studio. Ask its owner to start it with "
                           "--allow-url-import, or download the files and drop them here.")
    if not network_allowed():
        raise ImportFailed("Morph is offline (network turned off), so links can't be fetched; drop the files instead.")
    p = plan(url)
    os.makedirs(dest, exist_ok=True)
    deadline = time.time() + OVERALL_S
    if p["kind"] == "github_zip":
        info = _github_zip(p, dest, on_progress, deadline)
    elif p["kind"] == "github_raw":
        info = _github_raw(p, dest, on_progress, deadline)
    else:
        info = _hf(p, dest, on_progress, deadline)
    if not info["files"]:
        raise ImportFailed("Nothing was downloaded from that link.")
    info.update(url=url, source_url=p["source_url"])
    return info
