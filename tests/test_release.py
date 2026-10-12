"""Release plumbing: the Studio page loads nothing from other sites."""

import re
from pathlib import Path

from morph.studio import server

STATIC = Path(server.STATIC)


def test_the_page_loads_nothing_from_other_sites():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for text in (page, css):
        assert [u for u in re.findall(r"https?://[^\s\"'<>)]+", text) if "www.w3.org" not in u] == []
    faces = re.findall(r"url\((vendor/fonts/[^)]+)\)", css)
    assert len(faces) == 7
    for face in faces:
        assert (STATIC / face).read_bytes()[:4] == b"wOF2", face
    for name in ("OFL-Inter.txt", "OFL-JetBrainsMono.txt"):
        assert "SIL OPEN FONT LICENSE Version 1.1" in (STATIC / "vendor" / "fonts" / name).read_text(encoding="utf-8")
    csp = server.app_csp()
    assert "https:" not in csp and "'unsafe-hashes'" not in csp
    assert "font-src 'self' data:;" in csp


def test_fonts_are_served_as_fonts(tmp_path, monkeypatch):
    from test_studio_security import _serve, make_project
    import http.client

    from morph import keys
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    st = server.Studio(make_project(tmp_path / "projects"), str(tmp_path / "projects"))
    httpd = _serve(st, None)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("GET", "/static/vendor/fonts/inter-latin-400.woff2")
        res = conn.getresponse()
        body = res.read()
        conn.close()
        assert res.status == 200 and res.getheader("Content-Type") == "font/woff2" and body[:4] == b"wOF2"
    finally:
        httpd.shutdown()
        httpd.server_close()
