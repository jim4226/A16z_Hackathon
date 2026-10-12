"""Import treats every upload, URL, URDF, zip and mesh as hostile (contracts section 9); offline, no keys."""

import io
import os
import stat
import struct
import tarfile
import zipfile

import pytest

from morph.importers import ImportFailed, common, fetch, mjcf, project_zip, robots, urdf, xacro_sandbox, zipsafe
from morph.importers.session import Session
from test_import_fixtures import arm_urdf, morph_export, stl

# ------------------------------------------------------------------------------------------- zips


def _zip(path, entries, symlink=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
        if symlink:
            zi = zipfile.ZipInfo(symlink[0])
            zi.create_system = 3
            zi.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(zi, symlink[1])
    return str(path)


@pytest.mark.parametrize("bad", ["../../escape.txt", "/abs/escape.txt", "C:\\win\\escape.txt", "a/../../x.txt", "a.urdf:stream"])
def test_zip_slip_and_absolute_paths_refuse_the_archive(tmp_path, bad):
    z = _zip(tmp_path / "evil.zip", {"ok/a.urdf": "<robot/>", bad: "x"})
    out = tmp_path / "out"
    with pytest.raises(ImportFailed):
        zipsafe.extract_zip(z, str(out))
    assert not (tmp_path / "escape.txt").exists() and not os.path.exists("/abs/escape.txt")


def test_zip_symlinks_and_nested_archives_are_skipped_not_written(tmp_path):
    z = _zip(tmp_path / "s.zip", {"robot/a.urdf": "<robot/>", "robot/inner.zip": b"PK\x05\x06" + b"\0" * 18,
                                  "__MACOSX/robot/._a.urdf": "junk", "robot/.git/config": "secret"},
             symlink=("robot/link_to_etc", "/etc"))
    files, warns = zipsafe.extract_zip(z, str(tmp_path / "out"))
    assert files == ["robot/a.urdf"]
    assert any("symlink" in w for w in warns) and any("nested archive" in w for w in warns)
    assert not os.path.lexists(tmp_path / "out" / "robot" / "link_to_etc")


def test_zip_bomb_ratio_is_refused(tmp_path):
    z = tmp_path / "bomb.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.bin", b"\0" * (12 << 20))          # 12 MB of zeros compresses ~1000x
    with pytest.raises(ImportFailed, match="zip bomb"):
        zipsafe.extract_zip(str(z), str(tmp_path / "out"))
    assert not (tmp_path / "out" / "zeros.bin").exists()


def test_zip_entry_count_total_and_file_caps(tmp_path):
    z = _zip(tmp_path / "many.zip", {f"f{i}.txt": "x" for i in range(30)})
    with pytest.raises(ImportFailed, match="entries"):
        zipsafe.extract_zip(z, str(tmp_path / "a"), {"zip_entries": 20})
    z2 = _zip(tmp_path / "big.zip", {"a.bin": os.urandom(4096)})
    with pytest.raises(ImportFailed, match="per-file limit"):
        zipsafe.extract_zip(z2, str(tmp_path / "b"), {"zip_file": 1024})
    with pytest.raises(ImportFailed, match="limit"):
        zipsafe.extract_zip(z2, str(tmp_path / "c"), {"zip_total": 1024})


def test_zip_lying_size_header_is_caught_while_streaming(tmp_path):
    z = tmp_path / "liar.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("a.bin", b"A" * 5000)
    data = bytearray(z.read_bytes())
    cd = data.rfind(b"PK\x01\x02")
    data[cd + 24:cd + 28] = (100).to_bytes(4, "little")       # central directory claims 100 bytes
    lh = data.find(b"PK\x03\x04")
    data[lh + 22:lh + 26] = (100).to_bytes(4, "little")
    z.write_bytes(bytes(data))
    with pytest.raises(ImportFailed):
        zipsafe.extract_zip(str(z), str(tmp_path / "out"))


def test_tar_links_and_traversal(tmp_path):
    t = tmp_path / "t.tar.gz"
    with tarfile.open(t, "w:gz") as tf:
        info = tarfile.TarInfo("robot/a.urdf")
        info.size = 8
        tf.addfile(info, io.BytesIO(b"<robot/>"))
        ln = tarfile.TarInfo("robot/passwd")
        ln.type, ln.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tf.addfile(ln)
    files, warns = zipsafe.extract_tar(str(t), str(tmp_path / "out"))
    assert files == ["robot/a.urdf"] and any("links" in w for w in warns)
    t2 = tmp_path / "t2.tar"
    with tarfile.open(t2, "w") as tf:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ImportFailed):
        zipsafe.extract_tar(str(t2), str(tmp_path / "out2"))
    assert not (tmp_path / "escape.txt").exists()


def test_tar_members_that_are_skipped_still_count_toward_the_limit(tmp_path):
    t = tmp_path / "t.tar.gz"
    with tarfile.open(t, "w:gz") as tf:
        info = tarfile.TarInfo("robot/.cache/blob")         # hidden: skipped, but tarfile inflates it to get past
        info.size = 5000
        tf.addfile(info, io.BytesIO(b"\0" * 5000))
        info = tarfile.TarInfo("robot/a.urdf")
        info.size = 8
        tf.addfile(info, io.BytesIO(b"<robot/>"))
    with pytest.raises(ImportFailed, match="unpacks to more than"):
        zipsafe.extract_tar(str(t), str(tmp_path / "out"), limits={"zip_total": 4000})
    assert zipsafe.extract_tar(str(t), str(tmp_path / "out2"))[0] == ["robot/a.urdf"]


def test_windows_device_names_and_trailing_dots(tmp_path, monkeypatch):
    assert common.windows_unsafe("meshes/con.stl") and common.windows_unsafe("a/NUL") and common.windows_unsafe("a./b")
    assert not common.windows_unsafe("meshes/console.stl") and not common.windows_unsafe("a/b.stl")
    z = _zip(tmp_path / "w.zip", {"robot/a.urdf": "<robot/>", "robot/aux.stl": "solid x"})
    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(ImportFailed, match="Windows can't store"):
        common.safe_relpath("robot/con.stl")
    files, warns = zipsafe.extract_zip(z, str(tmp_path / "out"))
    monkeypatch.undo()
    assert files == ["robot/a.urdf"] and any("aux.stl" in w for w in warns)


# ------------------------------------------------------------------------------------------- XML


@pytest.mark.parametrize("doc", [
    '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><robot name="&x;"/>',
    '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]><robot name="&b;"/>',
    '<!doctype robot><robot name="r"/>',
])
def test_xml_doctype_and_entities_are_refused(tmp_path, doc):
    p = tmp_path / "evil.urdf"
    p.write_text(doc, encoding="utf-8")
    with pytest.raises(ImportFailed, match="DOCTYPE or ENTITY"):
        common.parse_xml(str(p))
    with pytest.raises(ImportFailed):
        urdf.normalize(str(p), str(tmp_path), str(tmp_path / "out"))


def test_utf16_doctype_is_also_refused(tmp_path):
    p = tmp_path / "u16.urdf"
    p.write_bytes('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY x "y">]><robot/>'.encode("utf-16"))
    with pytest.raises(ImportFailed, match="DOCTYPE"):
        common.parse_xml(str(p))


def test_xml_encodings_that_hide_a_doctype_are_refused(tmp_path):
    doc = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY x "y">]><robot name="&x;"/>'
    for i, data in enumerate([doc.encode("utf-16-le"), doc.encode("utf-16-be"),                # no byte order mark
                              b"\xef\xbb\xbf" + doc.replace("UTF-16", "UTF-8").encode("utf-8")]):
        p = tmp_path / f"enc{i}.urdf"
        p.write_bytes(data)
        with pytest.raises(ImportFailed, match="DOCTYPE"):
            common.parse_xml(str(p))
    ebcdic = b'<?xml version="1.0" encoding="cp037"?>' + '<!DOCTYPE r><robot/>'.encode("cp037")
    p = tmp_path / "ebcdic.urdf"
    p.write_bytes(ebcdic)
    with pytest.raises(ImportFailed, match="cp037 encoding; save it as UTF-8"):
        common.parse_xml(str(p))
    p.write_bytes('<?xml version="1.0" encoding="ISO-8859-1"?><robot name="caf\u00e9"/>'.encode("latin-1"))
    assert common.parse_xml(str(p)).get("name") == "caf\u00e9"


def test_xml_meshes_are_checked_and_unreadable_formats_say_what_they_need(tmp_path, monkeypatch):
    with pytest.raises(ImportFailed, match="DOCTYPE"):
        urdf._check_xml_mesh(b'<?xml version="1.0"?><!DOCTYPE c [<!ENTITY x "y">]><COLLADA/>', "dae", "arm.dae")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", '<?xml version="1.0"?><!DOCTYPE m [<!ENTITY x "y">]><model/>')
    with pytest.raises(ImportFailed, match="DOCTYPE"):
        urdf._check_xml_mesh(buf.getvalue(), "3mf", "egg.3mf")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("3D/3dmodel.model", b" " * (20 << 20))
    with pytest.raises(ImportFailed, match="far more data"):
        urdf._check_xml_mesh(buf.getvalue(), "3mf", "bomb.3mf")
    import trimesh
    p = tmp_path / "egg.3mf"
    p.write_bytes(buf.getvalue())
    monkeypatch.setattr(trimesh, "available_formats", lambda: {"stl", "obj", "glb"})
    with pytest.raises(ImportFailed, match="lxml and networkx"):
        urdf.load_mesh(str(p), "3mf")


def test_oversize_xml_is_refused(tmp_path, monkeypatch):
    p = tmp_path / "big.urdf"
    p.write_text("<robot name='r'>" + "<!-- pad -->" * 200 + "</robot>", encoding="utf-8")
    monkeypatch.setitem(common.LIMITS, "xml_bytes", 1000)
    with pytest.raises(ImportFailed, match="limit"):
        common.parse_xml(str(p))


# ------------------------------------------------------------------------------------------- meshes


def test_mesh_face_cap_and_malformed_stl(tmp_path, monkeypatch):
    big = stl(str(tmp_path / "b" / "big.stl"), (10, 10, 10))
    monkeypatch.setitem(common.LIMITS, "mesh_faces", 10)                 # a box has 12 triangles
    with pytest.raises(ImportFailed, match="triangles"):
        urdf.load_mesh(big, "stl")
    monkeypatch.setitem(common.LIMITS, "mesh_faces", 1_500_000)
    bad = tmp_path / "bad.stl"
    bad.write_bytes(b"\0" * 80 + struct.pack("<I", 2 ** 31 - 1) + b"\0" * 50)   # count lies, size doesn't match
    with pytest.raises(ImportFailed, match="not a valid STL"):
        urdf.load_mesh(str(bad), "stl")
    junk = tmp_path / "junk.ply"
    junk.write_bytes(b"ply\nformat binary_little_endian 1.0\nelement vertex 2147483647\nproperty float x\nend_header\n" + b"\0" * 64)
    with pytest.raises(ImportFailed, match="not a valid PLY"):
        urdf.load_mesh(str(junk), "ply")


def test_gltf_buffer_outside_folder_is_refused(tmp_path):
    p = tmp_path / "m.gltf"
    p.write_text('{"asset": {"version": "2.0"}, "buffers": [{"uri": "../../etc/passwd", "byteLength": 10}], "meshes": []}',
                 encoding="utf-8")
    with pytest.raises(ImportFailed, match="outside its folder"):
        urdf.load_mesh(str(p), "gltf")


def test_mesh_paths_never_leave_the_bundle(tmp_path):
    bundle = tmp_path / "bundle"
    secret = stl(str(tmp_path / "outside" / "secret.stl"))
    urdf_path = arm_urdf(str(bundle))
    text = open(urdf_path, encoding="utf-8").read()
    text = text.replace("meshes/base_link.stl", "../outside/secret.stl", 1)
    text = text.replace("meshes/shoulder.stl", f"file://{secret}", 1)
    text = text.replace("meshes/upper_arm.stl", os.path.abspath(secret), 1)
    with open(urdf_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    rep = urdf.normalize(urdf_path, str(bundle), str(tmp_path / "out"))
    missing = {m["ref"] for m in rep["meshes"]["missing"]}
    assert "../outside/secret.stl" in missing and f"file://{secret}" in missing and os.path.abspath(secret) in missing
    assert "3 of 7 meshes missing" in rep["warnings"][0]
    assert not any("secret" in f for f in os.listdir(tmp_path / "out" / "meshes"))


# ------------------------------------------------------------------------------------------- uploads


@pytest.mark.parametrize("rel", ["../x.stl", "/etc/x", "a/../../x", "C:/x", "a\\..\\..\\x", ".git/config", "a/.hidden",
                                 "a//b", "nul\x00.stl", "x" * 2000])
def test_upload_paths_are_validated(tmp_path, rel):
    s = Session.create(str(tmp_path))
    with pytest.raises(ImportFailed):
        s.add_file(rel, io.BytesIO(b"abc"), 3)
    assert os.listdir(s.files) == []


def test_upload_caps_and_cut_off_uploads(tmp_path, monkeypatch):
    s = Session.create(str(tmp_path))
    monkeypatch.setitem(common.LIMITS, "upload_total", 10)
    s.add_file("a.txt", io.BytesIO(b"12345"), 5)
    with pytest.raises(ImportFailed, match="limit"):
        s.add_file("b.txt", io.BytesIO(b"123456"), 6)
    monkeypatch.setitem(common.LIMITS, "upload_total", 1000)
    with pytest.raises(ImportFailed, match="cut off"):
        s.add_file("c.txt", io.BytesIO(b"12"), 50)
    assert sorted(os.listdir(s.files)) == ["a.txt"]
    assert s.state()["bytes"] == 5
    with pytest.raises(ImportFailed):
        Session.open("../../etc", str(tmp_path))


def test_upload_file_and_folder_name_clashes_are_refused_cleanly(tmp_path):
    s = Session.create(str(tmp_path))
    s.add_file("robot/a", io.BytesIO(b"12"), 2)
    with pytest.raises(ImportFailed, match="clashes"):
        s.add_file("robot/a/b.stl", io.BytesIO(b"123"), 3)
    with pytest.raises(ImportFailed, match="clashes"):
        s.add_file("robot", io.BytesIO(b"1"), 1)
    st = s.state()
    assert st["files"] == {"robot/a": 2} and st["bytes"] == 2


# ------------------------------------------------------------------------------------------- URL fetch


@pytest.mark.parametrize("url", ["http://github.com/a/b", "https://evil.example/a", "https://127.0.0.1/a",
                                 "https://github.com.evil.example/a/b", "https://user:pw@github.com/a/b",
                                 "https://github.com:8443/a/b", "file:///etc/passwd", "https://[::1]/x",
                                 "https://notgithub.com/a/b", "https://hf.co.evil.example/x"])
def test_url_allowlist(url):
    with pytest.raises(ImportFailed):
        fetch.check_url(url)


def test_allowlisted_hosts():
    for u in ("https://github.com/a/b", "https://raw.githubusercontent.com/a/b/main/x.urdf",
              "https://huggingface.co/a/b", "https://cdn-lfs.hf.co/x", "https://codeload.github.com/a/b/zip/main"):
        assert fetch.check_url(u).hostname


@pytest.mark.parametrize("url", ["https://github.com/x/..", "https://github.com/../b", "https://codeload.github.com/a/../zip/main",
                                 "https://codeload.github.com/a/%2e%2e/zip/main", "https://raw.githubusercontent.com/x/../main/a.urdf",
                                 "https://raw.githubusercontent.com/x/y/main/../../a.urdf", "https://huggingface.co/x/..",
                                 "https://raw.githubusercontent.com/x/y/main/.git/config"])
def test_link_segments_cannot_name_parent_or_hidden_folders(url):
    with pytest.raises(ImportFailed):
        fetch.plan(url)


def test_downloaded_folders_are_never_hidden():
    assert fetch._local(".github") == "github" and fetch._local("..") == "repo" and fetch._local("SO-ARM100") == "SO-ARM100"
    assert fetch.plan("https://github.com/a/.github")["repo"] == ".github"


def _fake_dns(monkeypatch, table):
    def gai(host, port, *a, **k):
        if host not in table:
            raise OSError("no such host")
        return [(2, 1, 6, "", (ip, port)) for ip in table[host]]
    monkeypatch.setattr(fetch.socket, "getaddrinfo", gai)


@pytest.mark.parametrize("ip", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.2", "::1", "fd00::1", "0.0.0.0",
                                "100.64.0.1", "::ffff:10.0.0.1"])
def test_private_addresses_are_refused(monkeypatch, ip):
    _fake_dns(monkeypatch, {"github.com": ["140.82.112.3", ip]})
    with pytest.raises(ImportFailed, match="private or local"):
        fetch.resolve_public("github.com")


class _Resp:
    def __init__(self, status, location=None, body=b""):
        self.status, self._loc, self._body = status, location, io.BytesIO(body)

    def getheader(self, k, d=None):
        return {"Location": self._loc, "Content-Length": str(len(self._body.getvalue()))}.get(k, d)

    def read(self, n=-1):
        return self._body.read(n)

    def close(self):
        pass


class _Conn:
    script = []

    def __init__(self, host):
        self.host = host

    def request(self, method, path, headers=None):
        self.path = path

    def getresponse(self):
        return _Conn.script.pop(0)

    def close(self):
        pass


def test_redirects_are_rechecked_and_capped(monkeypatch):
    _fake_dns(monkeypatch, {"github.com": ["140.82.112.3"], "codeload.github.com": ["140.82.114.10"]})
    monkeypatch.setattr(fetch, "_connect", lambda host, timeout: _Conn(host))
    _Conn.script = [_Resp(302, "https://169.254.169.254/latest/meta-data")]
    with pytest.raises(ImportFailed):
        fetch.open_url("https://github.com/a/b/archive/HEAD.zip")
    _Conn.script = [_Resp(302, "http://codeload.github.com/a/b/zip/main")]
    with pytest.raises(ImportFailed, match="https"):
        fetch.open_url("https://github.com/a/b/archive/HEAD.zip")
    _Conn.script = [_Resp(301, "https://evil.example/x")]
    with pytest.raises(ImportFailed, match="not on the import list"):
        fetch.open_url("https://github.com/a/b/archive/HEAD.zip")
    _Conn.script = [_Resp(302, "https://github.com/a/b/x")] * 4 + [_Resp(200, body=b"ok")]
    with pytest.raises(ImportFailed, match="redirects"):
        fetch.open_url("https://github.com/a/b/archive/HEAD.zip")
    _Conn.script = [_Resp(302, "https://codeload.github.com/a/b/zip/main"), _Resp(200, body=b"PK")]
    final, resp = fetch.open_url("https://github.com/a/b/archive/HEAD.zip")
    assert final == "https://codeload.github.com/a/b/zip/main" and resp.read() == b"PK"


def test_redirect_to_a_host_resolving_privately_is_refused(monkeypatch):
    _fake_dns(monkeypatch, {"github.com": ["140.82.112.3"], "codeload.github.com": ["10.1.2.3"]})
    calls = []

    def connect(host, timeout):
        fetch.resolve_public(host)           # what the real _connect does before any connection
        calls.append(host)
        return _Conn(host)

    monkeypatch.setattr(fetch, "_connect", connect)
    _Conn.script = [_Resp(302, "https://codeload.github.com/a/b/zip/main"), _Resp(200, body=b"PK")]
    with pytest.raises(ImportFailed, match="private or local"):
        fetch.open_url("https://github.com/a/b/archive/HEAD.zip")
    assert calls == ["github.com"]


def test_download_size_cap(monkeypatch, tmp_path):
    _fake_dns(monkeypatch, {"raw.githubusercontent.com": ["185.199.108.133"]})
    monkeypatch.setattr(fetch, "_connect", lambda host, timeout: _Conn(host))
    _Conn.script = [_Resp(200, body=b"x" * 5000)]
    with pytest.raises(ImportFailed, match="limit"):
        fetch.download("https://raw.githubusercontent.com/a/b/main/x.stl", str(tmp_path / "x.stl"), 1000)
    assert not (tmp_path / "x.stl").exists()


def test_url_import_off_in_shared_mode_and_offline(tmp_path, monkeypatch):
    with pytest.raises(ImportFailed, match="--allow-url-import"):
        fetch.fetch("https://github.com/a/b", str(tmp_path), allow=False)
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    with pytest.raises(ImportFailed, match="offline"):
        fetch.fetch("https://github.com/a/b", str(tmp_path))


# ------------------------------------------------------------------------------------------- robots, projects


@pytest.mark.parametrize("src", ["../so101", "/tmp", "so101/../x", ".robots", "", "SO101", "a" * 60, "so101\x00", None, 5])
def test_robot_source_must_be_an_id(src):
    with pytest.raises((ValueError, FileNotFoundError)):
        robots.resolve(src)


def test_user_robot_profile_cannot_point_outside_its_folder(tmp_path):
    home = tmp_path / "home"
    d = home / ".robots" / "evil"
    d.mkdir(parents=True)
    (d / "morph_profile.json").write_text('{"urdf": "/etc/passwd"}', encoding="utf-8")
    with pytest.raises(ValueError):
        robots.resolve("evil", home_dir=str(home))
    (d / "morph_profile.json").write_text('{"urdf": "../../x.urdf"}', encoding="utf-8")
    with pytest.raises(ValueError):
        robots.resolve("evil", home_dir=str(home))


def test_project_zip_refusals(tmp_path):
    from morph.spec import Design

    good = Design().to_dict()
    bad_entry = tmp_path / "slip.zip"
    with zipfile.ZipFile(bad_entry, "w") as z:
        z.writestr("design.json", "{}")
        z.writestr("../../evil.txt", "x")
    with pytest.raises(ImportFailed):
        project_zip.inspect(str(bad_entry))
    link = _zip(tmp_path / "link.zip", {"design.json": "{}"}, symlink=("cad/robot.glb", "/etc/passwd"))
    with pytest.raises(ImportFailed, match="symlink"):
        project_zip.inspect(link)
    notobj = morph_export(str(tmp_path / "list.zip"), [1, 2])
    with pytest.raises(ImportFailed, match="JSON object"):
        project_zip.inspect(notobj)
    traversal = morph_export(str(tmp_path / "src.zip"), {**good, "arm": {**good["arm"], "source": "../../etc"}})
    with pytest.raises(ImportFailed, match="robot id"):
        project_zip.inspect(traversal)
    unknown = morph_export(str(tmp_path / "unk.zip"), {**good, "arm": {**good["arm"], "source": "not-installed"}})
    with pytest.raises(ImportFailed, match="isn't in the export"):
        project_zip.plan(unknown, home_dir=str(tmp_path / "home"))
    wrongtype = morph_export(str(tmp_path / "wt.zip"), {**good, "object": "egg"})
    with pytest.raises(ImportFailed):
        project_zip.inspect(wrongtype)


def test_bundled_robot_meshes_must_be_inside(tmp_path):
    from morph.spec import Design

    d = Design().with_changes({"arm.source": "mine"}).to_dict()
    urdf_text = '<robot name="r"><link name="a"><visual><geometry><mesh filename="../../../etc/passwd"/></geometry></visual></link></robot>'
    z = morph_export(str(tmp_path / "rb.zip"), d, robot={
        "robot/morph_profile.json": '{"urdf": "robot.urdf", "actuators": {}, "arm_joints": [], "gripper_joint": "g"}',
        "robot/robot.urdf": urdf_text})
    with pytest.raises(ImportFailed, match="meshes must be"):
        project_zip.plan(z, home_dir=str(tmp_path / "home"))
    assert not (tmp_path / "home" / ".robots").exists() or not os.listdir(tmp_path / "home" / ".robots")


# ------------------------------------------------------------------------------------------- MJCF, xacro


def test_mjcf_file_references_must_stay_inside(tmp_path):
    stl(str(tmp_path / "outside.stl"))
    b = tmp_path / "b"
    b.mkdir()
    (b / "r.xml").write_text('<mujoco><asset><mesh name="m" file="../outside.stl"/></asset><worldbody/></mujoco>', encoding="utf-8")
    with pytest.raises(ImportFailed, match="outside"):
        mjcf.check_refs(str(b / "r.xml"), str(b))
    (b / "r2.xml").write_text('<mujoco><compiler meshdir="/etc"/><worldbody/></mujoco>', encoding="utf-8")
    with pytest.raises(ImportFailed, match="outside"):
        mjcf.check_refs(str(b / "r2.xml"), str(b))
    (b / "r3.xml").write_text('<mujoco><include file="/etc/hosts"/></mujoco>', encoding="utf-8")
    with pytest.raises(ImportFailed):
        mjcf.check_refs(str(b / "r3.xml"), str(b))


def test_xacro_missing_gives_the_install_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(xacro_sandbox, "_xacro_paths", lambda: None)
    p = tmp_path / "r.urdf.xacro"
    p.write_text('<robot xmlns:xacro="http://www.ros.org/wiki/xacro" name="r"/>', encoding="utf-8")
    with pytest.raises(ImportFailed, match=r'pip install "morph\[xacro\]"'):
        xacro_sandbox.expand(str(p), str(tmp_path))


XACRO_DIRS = [d for d in os.environ.get("MORPH_TEST_XACRO_PATH", "").split(os.pathsep) if d and os.path.isdir(os.path.join(d, "xacro"))]


def _with_xacro(monkeypatch):
    if xacro_sandbox.available():
        return
    if not XACRO_DIRS:
        pytest.skip("xacro is not installed (optional extra; or point MORPH_TEST_XACRO_PATH at a folder holding it)")
    monkeypatch.setattr(xacro_sandbox, "_xacro_paths", lambda: XACRO_DIRS)


def test_xacro_sandbox_blocks_env_includes_and_runaway_macros(tmp_path, monkeypatch):
    _with_xacro(monkeypatch)
    monkeypatch.setenv("MORPH_TEST_SECRET", "s3cr3t-value")
    ns = 'xmlns:xacro="http://www.ros.org/wiki/xacro"'
    cases = {
        "env.xacro": f'<robot {ns} name="r"><link name="a$(optenv MORPH_TEST_SECRET)"/><link name="b$(env HOME)"/></robot>',
        "inc.xacro": f'<robot {ns} name="r"><xacro:include filename="/etc/hostname"/></robot>',
        "yaml.xacro": f'<robot {ns} name="r"><xacro:property name="p" value="${{xacro.load_yaml(\'/etc/passwd\')}}"/>'
                      '<link name="${p}"/></robot>',
        "bomb.xacro": f'<robot {ns} name="r">' + "".join(f'<xacro:macro name="m{i}"><xacro:m{i + 1}/><xacro:m{i + 1}/></xacro:macro>'
                                                          for i in range(40)) + '<xacro:macro name="m40"><link name="x"/></xacro:macro><xacro:m0/></robot>',
    }
    for name, text in cases.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    out = xacro_sandbox.expand(str(tmp_path / "env.xacro"), str(tmp_path))
    assert "s3cr3t" not in out and '<link name="a"/>' in out and '<link name="b"/>' in out
    for name in ("inc.xacro", "yaml.xacro"):
        with pytest.raises(ImportFailed):
            xacro_sandbox.expand(str(tmp_path / name), str(tmp_path))
    with pytest.raises(ImportFailed, match="longer than"):
        xacro_sandbox.expand(str(tmp_path / "bomb.xacro"), str(tmp_path), timeout=3)


def test_xacro_find_resolves_inside_the_bundle(tmp_path, monkeypatch):
    _with_xacro(monkeypatch)
    pkg = tmp_path / "my_robot_description"
    (pkg / "urdf").mkdir(parents=True)
    (pkg / "package.xml").write_text("<package><name>my_robot_description</name></package>", encoding="utf-8")
    ns = 'xmlns:xacro="http://www.ros.org/wiki/xacro"'
    (pkg / "urdf" / "parts.xacro").write_text(f'<robot {ns}><xacro:macro name="part"><link name="p"/></xacro:macro></robot>', encoding="utf-8")
    (pkg / "urdf" / "r.urdf.xacro").write_text(
        f'<robot {ns} name="r"><xacro:include filename="$(find my_robot_description)/urdf/parts.xacro"/><xacro:part/>'
        f'<xacro:include filename="$(find other_pkg)/x.xacro" optional="true"/></robot>', encoding="utf-8")
    out = xacro_sandbox.expand(str(pkg / "urdf" / "r.urdf.xacro"), str(tmp_path))
    assert '<link name="p"/>' in out
    assert any("other_pkg" in w for w in xacro_sandbox.expand.warnings)
