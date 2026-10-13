"""Expand a .xacro robot in a sandboxed subprocess (optional extra: pip install "morph[xacro]").

  xml_text = expand("open_manipulator_x.urdf.xacro", bundle="work/files/open_manipulator")

xacro evaluates expressions and reads files, so it never runs in the Studio process (contracts section 9):
* a child Python (-I: no user site, no current-directory imports) with an argument list, never a shell;
* env={} plus PATH (and SYSTEMROOT on Windows, which Python needs to start), so $(env ...) / $(optenv ...)
  can't read API keys; the child also answers them with "" itself;
* $(find pkg), <xacro:include> and xacro.load_yaml only reach files inside the bundle (package.xml <name> or a
  folder named pkg); a missing package becomes a placeholder path and a warning;
* ${...} and $(eval ...) expressions are parsed first and refused unless they are plain arithmetic, names,
  calls, subscripts and attributes that don't start with "_" or reach frames or code (gi_frame, f_globals...),
  and they run without Python builtins. xacro's own $(eval) runs with all builtins (exec, getattr), and ${...}
  can reach module globals through a generator's frame, so neither is left as shipped;
* 20 s timeout, 1 GB address space (RLIMIT_AS, POSIX), 20 MB output cap, DOCTYPE/ENTITY refused.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading

from .common import LIMITS, MB, ImportFailed, check_xml_text, contained

MISSING = 'Expanding .xacro files needs the optional xacro extra: pip install "morph[xacro]" (or export a .urdf).'

_DRIVER = r'''
import builtins, glob, io, json, os, re, sys
try:
    import resource
    lim = int(sys.argv[4]) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
except Exception:
    pass
for p in json.loads(sys.argv[5]):
    sys.path.insert(0, p)
src, bundle, cap = os.path.realpath(sys.argv[1]), os.path.realpath(sys.argv[2]), int(sys.argv[3])
out, real_stdout = {"ok": False, "warnings": []}, sys.stdout
sys.stdout = io.StringIO()
class Capped(io.StringIO):
    def write(self, s):
        if self.tell() < 65536:
            return super().write(s[:65536])
        return len(s)
sys.stderr = Capped()
def inside(p):
    try:
        r = os.path.realpath(p)
        return os.path.commonpath([bundle, r]) == bundle
    except ValueError:
        return False
try:
    import xacro
    from xacro import substitution_args as sa
except Exception as exc:
    out["error"] = "missing"
    real_stdout.write(json.dumps(out)); sys.exit(0)
pkgs = {}
for dp, dns, fns in os.walk(bundle):
    dns[:] = [d for d in dns if not d.startswith(".") and not os.path.islink(os.path.join(dp, d))]
    if "package.xml" in fns:
        try:
            m = re.search(r"<name>\s*([^<\s]+)\s*</name>", open(os.path.join(dp, "package.xml"), encoding="utf-8", errors="replace").read(200000))
        except OSError:
            m = None
        pkgs.setdefault(m.group(1) if m else os.path.basename(dp), dp)
def find(pkg):
    if pkg in pkgs:
        return pkgs[pkg]
    for dp, dns, fns in os.walk(bundle):
        if os.path.basename(dp) == pkg:
            return dp
    out["warnings"].append("package %s is not in the folder; its files are skipped" % pkg)
    return os.path.join(bundle, "__missing__", pkg)
sa._eval_find = find
sa._eval_env = lambda name: ""
sa._eval_optenv = lambda name, default="": default
sa._eval_dict["find"] = find
sa._eval_dict["env"] = sa._eval_env
sa._eval_dict["optenv"] = sa._eval_optenv
_open = builtins.open
def guarded_open(file, mode="r", *a, **k):
    if isinstance(file, int) or any(c in mode for c in "wax+") or not inside(file):
        raise PermissionError("xacro may only read files inside the imported folder: %s" % file)
    return _open(file, mode, *a, **k)
builtins.open = guarded_open
io.open = guarded_open
_glob = glob.glob
glob.glob = lambda pat, *a, **k: [p for p in _glob(pat, *a, **k) if inside(p)]
xacro.glob.glob = glob.glob
_parse = xacro.parse
def parse(inp, filename=None):
    if inp is None and filename is not None:
        path = filename if os.path.isabs(filename) else os.path.join(getattr(xacro, "root_dir", "") or "", filename)
        try:
            if not inside(path):
                raise PermissionError(13, "include outside the imported folder", filename)
            with guarded_open(path, encoding="utf-8") as fh:
                text = fh.read(20 * 1024 * 1024 + 1)
        except OSError as e:          # same shape as xacro's own parse(), so optional includes are skipped
            try:
                xacro.filestack.pop()
            except Exception:
                pass
            raise xacro.XacroException("%s: %s" % (e.strerror or "cannot read", e.filename or filename), exc=e)
        if re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I):
            raise xacro.XacroException("DOCTYPE/ENTITY is not allowed: %s" % filename)
        return _parse(text, filename)
    return _parse(inp, filename)
xacro.parse = parse
import ast, math
BAD_ATTR = re.compile(r"^(_|gi_|cr_|ag_|f_|tb_|co_|func_|im_)|^(mro|format|format_map)$")
OK = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.Store, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
      ast.IfExp, ast.Call, ast.keyword, ast.Attribute, ast.Subscript, ast.Slice, ast.Tuple, ast.List, ast.Dict,
      ast.Set, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension, ast.JoinedStr,
      ast.FormattedValue, ast.Starred, ast.operator, ast.unaryop, ast.boolop, ast.cmpop, ast.expr_context)
def checked(expr):
    try:
        tree = ast.parse(str(expr).strip(), "<expression>", "eval")
    except (SyntaxError, ValueError):
        raise xacro.XacroException("invalid expression: %s" % str(expr)[:120])
    for node in ast.walk(tree):
        if not isinstance(node, OK) or (isinstance(node, ast.Attribute) and BAD_ATTR.search(node.attr)) or \
                (isinstance(node, ast.Name) and node.id.startswith("_")):
            raise xacro.XacroException("not allowed in an imported xacro (%s): %s" % (type(node).__name__, str(expr)[:120]))
    return compile(tree, "<expression>", "eval")
def safe_eval(expr, globals, locals=None):
    code = checked(expr)
    globals.update(__builtins__={})
    return eval(code, globals, locals)
xacro.safe_eval = safe_eval
for ns in (xacro._global_symbols.get("python"), xacro._global_symbols):
    if isinstance(ns, dict):
        for k in ("type", "vars"):
            dict.pop(ns, k, None)
SAFE = {"True": True, "False": False, "None": None, "int": int, "float": float, "str": str, "bool": bool,
        "abs": abs, "min": min, "max": max, "round": round, "len": len, "pi": math.pi}
def sa_eval(s, context):
    if "arg" not in context:
        context["arg"] = {}
    functions = dict(SAFE)
    functions.update((k, v) for k, v in sa._eval_dict.items() if not k.startswith("_"))
    functions["arg"] = lambda name: sa.convert_value(sa._eval_arg(name, args=context["arg"]), "auto")
    functions["dirname"] = lambda: sa._eval_dirname(context.get("filename"))
    return str(eval(checked(s), {"__builtins__": {}}, sa._DictWrapper(context["arg"], functions)))
if all(hasattr(sa, n) for n in ("_eval", "_eval_dict", "convert_value", "_eval_arg", "_eval_dirname", "_DictWrapper")):
    sa._eval = sa_eval
else:
    def no_eval(s, context):
        raise xacro.XacroException("$(eval ...) is not supported in imported xacro files with this xacro version")
    sa._eval = no_eval
try:
    doc = xacro.process_file(src, mappings={})
    text = doc.toprettyxml(indent="  ")
    if len(text) > cap:
        out["error"] = "the expanded robot is over %d MB" % (cap // 1048576)
    else:
        out.update(ok=True, xml=text)
except MemoryError:
    out["error"] = "ran out of memory (a macro that expands without end?)"
except Exception as exc:
    out["error"] = "%s: %s" % (exc.__class__.__name__, str(exc)[:300])
real_stdout.write(json.dumps(out))
'''


def _xacro_paths() -> list | None:
    """sys.path entries the child needs for xacro (and PyYAML), or None when xacro isn't installed."""
    spec = importlib.util.find_spec("xacro")
    if spec is None or not spec.origin:
        return None
    out = [os.path.dirname(os.path.dirname(spec.origin))]
    y = importlib.util.find_spec("yaml")
    if y is not None and y.origin:
        out.append(os.path.dirname(os.path.dirname(y.origin)))
    return out


def available() -> bool:
    return _xacro_paths() is not None


def expand(path: str, bundle: str, timeout: float = 20.0, mem_mb: int = 1024, max_out: int | None = None) -> str:
    """The expanded URDF text. Raises ImportFailed (with the install hint when xacro is missing)."""
    paths = _xacro_paths()
    if paths is None:
        raise ImportFailed(MISSING)
    if contained(bundle, path) is None:
        raise ImportFailed("The xacro file is outside the imported folder.")
    cap = max_out or LIMITS["xml_bytes"]
    with open(path, "rb") as fh:
        check_xml_text(fh.read(LIMITS["xml_bytes"] + 1), os.path.basename(path))
    env = {"PATH": os.environ.get("PATH", "")}
    if os.name == "nt" and os.environ.get("SYSTEMROOT"):
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    args = [sys.executable, "-I", "-c", _DRIVER, os.path.realpath(path), os.path.realpath(bundle), str(cap),
            str(mem_mb), json.dumps(paths)]
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=env, cwd=os.path.realpath(bundle), close_fds=True)
    except OSError as exc:
        raise ImportFailed(f"Couldn't start the xacro sandbox ({exc}).") from None
    buf, over = [], [False]

    def reader():
        n = 0
        while True:
            chunk = proc.stdout.read(MB)
            if not chunk:
                break
            n += len(chunk)
            if n > cap * 2 + MB:            # JSON escaping can double the size; anything beyond is hostile
                over[0] = True
                proc.kill()
                break
            buf.append(chunk)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise ImportFailed(f"Expanding {os.path.basename(path)} took longer than {timeout:.0f} s; Morph stopped it "
                           "(a macro that never ends?). Export a .urdf instead.") from None
    t.join(5)
    if over[0]:
        raise ImportFailed(f"The expanded robot is over {cap // MB} MB.")
    try:
        res = json.loads(b"".join(buf).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ImportFailed(f"xacro failed on {os.path.basename(path)} (it stopped without a result; out of memory?).") from None
    if res.get("error") == "missing":
        raise ImportFailed(MISSING)
    if not res.get("ok"):
        raise ImportFailed(f"xacro couldn't expand {os.path.basename(path)}: {res.get('error')}")
    text = res["xml"]
    check_xml_text(text, os.path.basename(path))
    expand.warnings = res.get("warnings") or []
    return text


expand.warnings = []
