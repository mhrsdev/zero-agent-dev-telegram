"""Local admin GUI (/admin) — loopback-first, setup-token bootstrap,
scrypt passwords, signed sessions, CSRF, redacted views. Lite by design:
this is NOT Zero Dev Web."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from zero.manage.core.config import ConfigService, GroupPolicy, ZeroConfig

_SALT = b"zero-admin-v1"
_sessions: dict[str, float] = {}  # sid -> expiry (single-process)


def _home() -> Path:
    return Path(os.environ.get("ZERO_HOME", Path.home() / ".zero"))


def _cfgsvc() -> ConfigService:
    return ConfigService(_home())


def _admin_file() -> Path:
    return _home() / "admin.json"


def _ensure_setup_code() -> str:
    f = _home() / "setup-code.txt"
    if not f.exists():
        code = secrets.token_hex(4).upper()
        f.write_text(code, encoding="utf-8")
        os.chmod(f, 0o600)
    return f.read_text(encoding="utf-8").strip()


def _hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1)
    return f"{salt.hex()}${dk.hex()}"


def _verify_pw(pw: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split("$", 1)
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1)
        return hmac.compare_digest(calc.hex(), dk_hex)
    except Exception:  # noqa: BLE001 - malformed storage fails closed
        return False


def _session_cookie(request: Request) -> str | None:
    return request.cookies.get("zero_admin")


def _valid_session(request: Request) -> bool:
    sid = _session_cookie(request)
    if not sid:
        return False
    exp = _sessions.get(sid, 0)
    if exp < time.time():
        _sessions.pop(sid, None)
        return False
    _sessions[sid] = time.time() + 1800
    return True


def _new_session() -> tuple[str, str]:
    sid = secrets.token_urlsafe(32)
    _sessions[sid] = time.time() + 1800
    return sid, sid[:12]


def _csrf(sid: str) -> str:
    return hashlib.sha256(f"csrf:{sid}".encode()).hexdigest()[:32]


def _check_csrf(sid: str, token: str) -> bool:
    expected = _csrf(sid or "")
    return bool(token) and hmac.compare_digest(token, expected)


_BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zero Admin</title><style>
:root{--fg:#111;--bg:#fff;--muted:#666;--line:#e5e5e5;--ok:#0a7d33;--bad:#b3261e}
@media(prefers-color-scheme:dark){:root{--fg:#eee;--bg:#141414;--muted:#999;--line:#333}}
body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0}
header{padding:.6rem 1rem;border-bottom:1px solid var(--line);display:flex;gap:1rem;align-items:center}
a{color:inherit;text-decoration:none;margin-right:.8rem}
main{max-width:900px;margin:1rem auto;padding:0 1rem}
table{width:100%;border-collapse:collapse}td,th{padding:.45rem;border-bottom:1px solid var(--line);text-align:left}
input,button,select{font:inherit;padding:.4rem .6rem;background:var(--bg);color:var(--fg);
 border:1px solid var(--muted);border-radius:6px}
button{cursor:pointer}.muted{color:var(--muted)}.ok{color:var(--ok)}.bad{color:var(--bad)}
.card{border:1px solid var(--line);border-radius:10px;padding:1rem;margin:.8rem 0}
code,.ltr{direction:ltr;unicode-bidi:isolate;font-family:ui-monospace,monospace}
form.inline{display:inline}
</style></head><body><header><strong>Zero Admin</strong>
<a href="/admin">Overview</a><a href="/admin/groups">Groups</a>
<a href="/admin/providers">Providers</a><a href="/admin/config">Config</a>
<form class="inline" method="post" action="/admin/logout">
<input type="hidden" name="csrf" value="{csrf}">
<button>Logout</button></form></header><main>{body}</main></body></html>"""


def _page(body: str, sid: str | None) -> HTMLResponse:
    csrf = _csrf(sid) if sid else ""
    return HTMLResponse(_BASE.replace("{csrf}", csrf).replace("{body}", body))


def _login_page(msg: str = "", need_setup: bool = False) -> HTMLResponse:
    kind = "setup code" if need_setup else "password"
    action = "/admin/login/bootstrap" if need_setup else "/admin/login"
    html = f"""<h2>Enter {kind}</h2>{f"<p class=bad>{msg}</p>" if msg else ""}
<form method="post" action="{action}">
<input type="password" name="secret" placeholder="{kind}" autofocus required>
<button>Login</button></form>
<p class=muted>Remote host? Use an SSH tunnel:
<code class=ltr>ssh -L 8787:127.0.0.1:8787 user@host</code></p>"""
    return HTMLResponse(_BASE.replace("{csrf}", "").replace("{body}", html))


def register_admin(app) -> None:
    """Mount /admin routes onto the running engine app."""
    router = APIRouter(prefix="/admin")

    @router.get("/login", response_class=HTMLResponse)
    def login_form():
        need = not _admin_file().exists()
        if need:
            _ensure_setup_code()
        return _login_page(need_setup=need)

    @router.post("/login/bootstrap")
    def login_bootstrap(secret: str = Form(...)):
        expected = _ensure_setup_code()
        if not hmac.compare_digest(secret.strip().upper(), expected):
            return _login_page("invalid setup code", need_setup=True)
        pw_page = """<h2>Set admin password</h2>
<form method="post" action="/admin/login/setpw">
<input type="password" name="pw" minlength="10" required placeholder="password (min 10)">
<input type="password" name="pw2" minlength="10" required placeholder="repeat">
<button>Save</button></form>"""
        return _page(pw_page, None)

    @router.post("/login/setpw")
    def set_password(pw: str = Form(""), pw2: str = Form("")):
        if pw != pw2 or len(pw) < 10:
            return _login_page("passwords must match and be >= 10 chars", need_setup=True)
        af = _admin_file()
        af.write_text(json.dumps({"password": _hash_pw(pw)}), encoding="utf-8")
        os.chmod(af, 0o600)
        (_home() / "setup-code.txt").unlink(missing_ok=True)
        sid, _ = _new_session()
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie("zero_admin", sid, httponly=True, samesite="strict")
        return resp

    @router.post("/login")
    def login(secret: str = Form("")):
        af = _admin_file()
        stored = ""
        if af.exists():
            stored = json.loads(af.read_text(encoding="utf-8")).get("password", "")
        if not stored or not _verify_pw(secret, stored):
            return _login_page("invalid password")
        sid, _ = _new_session()
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie("zero_admin", sid, httponly=True, samesite="strict")
        return resp

    @router.post("/logout")
    def logout(request: Request, csrf: str = Form("")):
        sid = request.cookies.get("zero_admin") or ""
        if _check_csrf(sid, csrf):
            _sessions.pop(sid, None)
        return RedirectResponse("/admin/login", status_code=303)

    # ---- authenticated pages -------------------------------------------
    class HTTPRedirect(Exception):
        def __init__(self, location: str) -> None:
            self.location = location

    def guard(request: Request):
        if not _valid_session(request):
            raise HTTPRedirect("/admin/login")
        return _session_cookie(request)

    @app.exception_handler(HTTPRedirect)
    def _redir(request: Request, exc: HTTPRedirect):
        from fastapi.responses import RedirectResponse as RR

        return RR(exc.location, status_code=303)

    @router.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        sid = guard(request)
        cfgsvc = _cfgsvc()
        cfg = cfgsvc.load() if cfgsvc.exists() else None
        rows = "".join(
            f"<tr><td class=ltr>{p.id}</td><td>{p.protocol}</td>"
            f"<td>{'yes' if p.enabled else 'no'}</td>"
            f"<td>{', '.join(p.models)}</td></tr>"
            for p in (cfg.providers if cfg else [])
        )
        groups = "".join(
            f"<tr><td class=ltr>{g.chat_id}</td><td>{g.title}</td>"
            f"<td>{'on' if g.enabled else 'off'}</td><td>{cfg.access.mode}</td></tr>"
            for g in (cfg.access.groups if cfg else [])
        )
        body = f"""
<div class=card><h3>Service</h3>
<p class=ok>engine running (this process)</p>
<p class=muted>GUI binds {cfg.server.host if cfg else "127.0.0.1"} — keep it loopback;
use SSH tunneling remotely.</p></div>
<div class=card><h3>Providers</h3><table>
<tr><th>id</th><th>protocol</th><th>enabled</th><th>models</th></tr>
{rows or "<tr><td colspan=4 class=muted>none configured — run wizard</td></tr>"}
</table></div>
<div class=card><h3>Groups & Access ({cfg.access.mode if cfg else "-"})</h3>
<table><tr><th>chat id</th><th>title</th><th>state</th><th>mode</th></tr>
{groups or "<tr><td colspan=4 class=muted>no groups yet</td></tr>"}
</table></div>"""
        return _page(body, sid)

    @router.get("/groups", response_class=HTMLResponse)
    def groups(request: Request):
        sid = guard(request)
        cfgsvc = _cfgsvc()
        cfg = cfgsvc.load() if cfgsvc.exists() else ZeroConfig()
        rows = "".join(
            f"<tr><td class=ltr>{g.chat_id}</td><td>{g.title}</td>"
            f"<td>{g.rate_limit_per_min}/min</td>"
            f"<td>{g.daily_token_budget:,}</td></tr>"
            for g in cfg.access.groups
        )
        body = f"""
<h3>Groups</h3>
<table><tr><th>chat id</th><th>title</th><th>rate</th><th>daily tokens</th></tr>
{rows or "<tr><td colspan=4 class=muted>none</td></tr>"}</table>
<h4>Add group (verified id only)</h4>
<form method="post" action="/admin/groups/add">
<input type="hidden" name="csrf" value="{_csrf(sid)}">
<input name="chat_id" placeholder="-1001234567890" required>
<input name="title" placeholder="title">
<input type="submit" value="Add"></form>"""
        return _page(body, sid)

    @router.post("/groups/add")
    def groups_add(
        request: Request, csrf: str = Form(""), chat_id: str = Form(""), title: str = Form("")
    ):
        sid = guard(request)
        if not _check_csrf(sid, csrf):
            return HTMLResponse("bad csrf", status_code=400)
        cfgsvc = _cfgsvc()
        cfg = cfgsvc.load()
        cfg.access.groups.append(GroupPolicy(chat_id=str(chat_id), title=title))
        if cfg.access.mode == "owner_only":
            cfg.access.mode = "groups"
        cfgsvc.save(cfg)
        return RedirectResponse("/admin/groups", status_code=303)

    @router.get("/providers", response_class=HTMLResponse)
    def providers_page(request: Request):
        sid = guard(request)
        cfgsvc = _cfgsvc()
        cfg = cfgsvc.load() if cfgsvc.exists() else ZeroConfig()
        items = (
            "".join(
                f"<li><span class=ltr>{p.id}</span> — {p.protocol}, "
                f"{len(p.models)} models, priority {p.fallback_priority}"
                f"</li>"
                for p in cfg.providers
            )
            or "<li class=muted>none</li>"
        )
        body = f"""
<h3>Providers</h3><ul>{items}</ul>
<p class=muted>Add via wizard (<code>/admin/wizard</code>) or CLI
(<code>zero providers add</code>). Keys are stored encrypted; the panel never
displays them.</p>"""
        return _page(body, sid)

    @router.get("/config", response_class=HTMLResponse)
    def config_page(request: Request):
        sid = guard(request)
        cfgsvc = _cfgsvc()
        data = cfgsvc.load().redacted_dict() if cfgsvc.exists() else {}
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
        body = f"<h3>Configuration (redacted)</h3><pre class=ltr>{pretty}</pre>"
        return _page(body, sid)

    app.include_router(router)
