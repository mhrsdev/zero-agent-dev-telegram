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
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)

from zero.manage.core.config import ConfigService, GroupPolicy, ZeroConfig
from zero.manage.services.wizard_forms import WIZARD_STEPS


def _stream_hub(app=None):
    """Process-wide execution stream hub (app.state first, lazy else)."""
    if app is not None:
        hub = getattr(app.state, "stream_hub", None)
        if hub is not None:
            return hub
    hub = getattr(register_admin, "_stream_hub", None)
    if hub is None:
        from zero.app.stream_hub import ExecutionStreamHub

        hub = ExecutionStreamHub()
        register_admin._stream_hub = hub  # type: ignore[attr-defined]
    return hub


def _ref_cls():
    import zero.domain.secrets as s

    return s.SecretReferenceId


def cfg_draft_data() -> dict:
    return _cfgsvc().load_draft()


def save_draft(d: dict) -> None:
    _cfgsvc().save_draft(d)


def cfgsvc_exists() -> bool:
    return _cfgsvc().exists()


def cfg_load():
    return _cfgsvc().load()


def cache_put(cfgsvc, report):
    from zero.manage.core.capabilities import CapabilityCache

    CapabilityCache(Path(_home())).put(report)


ORDER_LIST: list = list(WIZARD_STEPS)
STEP_ORDER_IDX: dict = {s: i for i, s in enumerate(ORDER_LIST)}

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
<a href="/admin/chat">Chat</a>
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


def _usage_summary(days: int = 30) -> list:
    try:
        from zero.app.services import build_services
        from zero.config import Settings
        from zero.persistence.connection import open_database
        from zero.persistence.migrations import apply_migrations

        settings = Settings.load()
        database = open_database(settings)
        apply_migrations(database)
        svc = build_services(settings, database)

        since = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() - days * 86400))
        conn = svc.database.connect()
        rows = conn.execute(
            "SELECT substr(created_at,1,10) day, provider, model,"
            " COUNT(*) requests, SUM(input_tokens) it, SUM(output_tokens) ot,"
            " SUM(CAST(estimated_cost_usd AS REAL)) cost"
            " FROM provider_usage WHERE created_at >= ?"
            " GROUP BY day, provider, model ORDER BY day DESC LIMIT 200",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def register_admin(app, services=None) -> None:
    """Mount /admin routes onto the running engine app."""
    _svc = services

    def _setup():
        from zero.manage.services.setup import SetupService

        store = None
        if _svc is not None:

            def store(name, stype, value):
                project = _ensure_project(_svc)
                ref = _svc.secrets.store(
                    project_id=project.id,
                    name=name,
                    secret_type=stype,
                    value=value,
                    actor_id=project.owner_user_id,
                )
                return ref.id.value

        return SetupService(_cfgsvc(), lambda: None, secret_store=store)

    def _ensure_project(svc):
        proj = getattr(app.state, "manage_project", None)
        if proj is not None:
            return proj
        for p in svc.identity.list_projects():
            if p.name == "Zero Management":
                app.state.manage_project = p
                return p
        op = svc.identity.create_user(display_name="Zero Operator")
        proj = svc.identity.create_project(owner_id=op.id, name="Zero Management")
        app.state.manage_project = proj
        return proj

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
        stream_panel = """
<div class=card><h3>Live execution stream</h3>
<form onsubmit="return watchExec(this)">
<input name="exec_id" placeholder="exec_…" required style="min-width:260px">
<button>Watch</button></form>
<pre class=ltr id=streamout style="white-space:pre-wrap;max-height:280px;overflow:auto"></pre>
<script>
function watchExec(f){
  const out=document.getElementById('streamout');out.textContent='';
  fetch('/admin/executions/'+encodeURIComponent(f.exec_id.value)+'/stream')
    .then(function(res){
      if(!res.ok||!res.body){out.textContent='stream unavailable ('+res.status+')';return null;}
      const reader=res.body.getReader();const dec=new TextDecoder();let buf='';
      function pump(){
        return reader.read().then(function(r){
          if(r.done)return;
          buf+=dec.decode(r.value,{stream:true});
          let i;
          while((i=buf.indexOf('\\n\\n'))>=0){
            const frame=buf.slice(0,i);buf=buf.slice(i+2);
            const line=frame.split('\\n').find(function(l){return l.indexOf('data: ')===0;});
            if(line){
              try{
                const ev=JSON.parse(line.slice(6));
                if(ev.type==='text_delta')out.textContent+=ev.text;
                else if(ev.type==='tool_call')out.textContent+='[tool:'+ev.name+'] ';
                else if(ev.type==='done')out.textContent+='\\n[done]';
              }catch(e){}
            }
          }
          return pump();
        });
      }
      return pump();
    });
  return false;
}
</script></div>"""
        body = f"""
<div class=card><h3>Service</h3>
<p class=ok>engine running (this process)</p>
<p class=muted>GUI binds {cfg.server.host if cfg else "127.0.0.1"} — keep it loopback;
use SSH tunneling remotely.</p></div>
{stream_panel}
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

    # ------------------------------------------------------------------
    # Wizard (drives the shared SetupService state machine)
    # ------------------------------------------------------------------

    def _field_html(field, value):
        v = "" if value is None else str(value)
        checked = " checked" if value is True else ""
        if field.kind == "password":
            return (
                '<input type="password" name="'
                + field.name
                + '" value="'
                + v
                + '" autocomplete="off">'
            )
        if field.kind == "bool":
            return (
                '<label><input type="checkbox" name="'
                + field.name
                + '" value="true"'
                + checked
                + "> enable</label>"
            )
        if field.kind == "select":
            opts = "".join(
                "<option" + (" selected" if o == v else "") + ">" + o + "</option>"
                for o in field.options
            )
            return '<select name="' + field.name + '">' + opts + "</select>"
        if field.kind == "int":
            return (
                '<input type="number" name="'
                + field.name
                + '" value="'
                + (v or str(field.default or 0))
                + '">'
            )
        req = " required" if field.required else ""
        return '<input type="text" name="' + field.name + '" value="' + v + '"' + req + ">"

    def _coerce(step_id, form):
        step = WIZARD_STEPS[step_id]
        out = {}
        for fdef in step.fields:
            raw = (form.get(fdef.name) or "").strip()
            if fdef.kind == "bool":
                out[fdef.name] = raw.lower() in ("true", "on", "1")
            elif fdef.kind == "int":
                try:
                    out[fdef.name] = int(raw) if raw else int(fdef.default or 0)
                except ValueError:
                    out[fdef.name] = fdef.default
            elif raw:
                out[fdef.name] = raw
            elif fdef.default is not None:
                out[fdef.name] = fdef.default
        return out

    @router.get("/wizard", response_class=HTMLResponse)
    def wizard_page(request: Request):
        sid = guard(request)
        svc = _setup()
        step_id = svc.current()
        step = WIZARD_STEPS[step_id]
        saved = cfg_draft_data().get("data", {}).get(step_id, {})
        fields_html = []
        for fd in step.fields:
            val = saved.get(fd.name, fd.default)
            hh = "<span class=muted>" + fd.help + "</span><br>" if fd.help else ""
            fields_html.append(
                "<p><label>"
                + fd.label
                + "</label><br>"
                + _field_html(fd, val)
                + "<br>"
                + hh
                + "</p>"
            )
        err_html = ""
        eq = request.query_params.get("err")
        if eq:
            errs = json.loads(eq)
            err_html = "".join("<p class=bad>! " + e + "</p>" for e in errs)
        back_btn = ""
        if STEP_ORDER_IDX.get(step_id, 0) > 0:
            back_btn = '<button name="action" value="back">Back</button>'
        skip_btn = '<button name="action" value="skip">Skip</button>' if step.optional else ""
        commit_btn = ""
        if step_id in {"final_validation", "backup_policy"}:
            commit_btn = '<button name="action" value="commit">Write configuration</button>'
        preview = ""
        if step_id == "final_validation":
            try:
                preview_obj = svc.build_preview().redacted_dict()
                preview = "<pre class=ltr>" + json.dumps(preview_obj, indent=2)[:4000] + "</pre>"
            except Exception as exc:  # noqa: BLE001
                preview = "<p class=bad>preview failed: " + str(exc) + "</p>"
        body = (
            "<h3>Wizard - "
            + step.title
            + " <span class=muted>(step: "
            + step_id
            + ")</span></h3>"
            + err_html
            + preview
            + '<form method="post" action="/admin/wizard/answer">'
            + '<input type="hidden" name="csrf" value="'
            + _csrf(sid)
            + '">'
            + '<input type="hidden" name="step" value="'
            + step_id
            + '">'
            + "".join(fields_html)
            + ("<p class=muted>No inputs for this step.</p>" if not fields_html else "")
            + '<button name="action" value="answer">Continue</button>'
            + back_btn
            + skip_btn
            + commit_btn
            + "</form><p class=muted>Draft auto-saves; resume anytime.</p>"
        )
        return _page(body, sid)

    @router.post("/wizard/answer")
    async def wizard_answer(request: Request):
        form = await request.form()
        csrf = str(form.get("csrf", ""))
        step_id = str(form.get("step", ""))
        action = str(form.get("action") or "answer")
        sid = request.cookies.get("zero_admin") or ""
        if not _check_csrf(sid, csrf):
            return HTMLResponse("bad csrf", status_code=400)
        svc = _setup()
        if action == "back":
            idx = STEP_ORDER_IDX.get(step_id, 0)
            draft = cfg_draft_data()
            draft["current_step"] = ORDER_LIST[max(0, idx - 1)]
            save_draft(draft)
            return RedirectResponse("/admin/wizard", status_code=303)
        if action == "commit":
            try:
                svc.commit()
            except Exception as exc:  # noqa: BLE001
                import urllib.parse as up

                return RedirectResponse("/admin/wizard?err=" + up.quote(str(exc)), status_code=303)
            return RedirectResponse("/admin?msg=config-written", status_code=303)
        value = _coerce(step_id, dict(form))
        result = svc.answer(step_id, value)
        if not result.ok:
            import urllib.parse as up

            return RedirectResponse(
                "/admin/wizard?err=" + up.quote(json.dumps(result.errors)), status_code=303
            )
        return RedirectResponse("/admin/wizard", status_code=303)

    @router.post("/wizard/reset")
    def wizard_reset(request: Request, csrf: str = Form("")):
        sid = guard(request)
        if not _check_csrf(sid, csrf):
            return HTMLResponse("bad csrf", status_code=400)
        _setup().reset()
        return RedirectResponse("/admin/wizard", status_code=303)

    @router.post("/providers/{provider_id}/test")
    def provider_test(provider_id: str):
        cfgsvc = _cfgsvc()
        cfg = cfg_load() if cfgsvc.exists() else None
        target = next((p for p in (cfg.providers if cfg else []) if p.id == provider_id), None)
        if target is None:
            return JSONResponse({"ok": False, "message": "unknown provider"}, status_code=404)
        key = ""
        retry_after = None
        if _svc is not None and target.api_key_ref:
            try:
                project = _ensure_project(_svc)
                key = _svc.secrets.resolve_value(
                    project_id=project.id,
                    secret_id=_ref_cls()(target.api_key_ref),
                    actor_id=project.owner_user_id,
                )
            except Exception:  # noqa: BLE001
                key = ""
        from zero.manage.core.capabilities import probe_capabilities

        model = (
            target.models[0]
            if target.models
            else ("claude-sonnet-4" if target.protocol == "anthropic" else "gpt-4o-mini")
        )
        report = probe_capabilities(
            protocol=target.protocol,
            base_url=target.base_url,
            api_key=key,
            model=model,
            provider_id=target.id,
        )
        caps = report.to_dict()
        message = "tool_calls=" + caps["tool_calls"] + " streaming=" + caps["streaming"]
        detail = caps.get("detail", {})
        for v in detail.values():
            sv = str(v)
            if "retry_after=" in sv:
                try:
                    retry_after = int(sv.split("retry_after=")[-1].split(")")[0])
                except ValueError:
                    retry_after = None
        ok = caps["tool_calls"] != "unsupported"
        cache_put(cfgsvc, report)
        return JSONResponse(
            {
                "ok": ok and "unavailable" not in (caps["tool_calls"], caps["streaming"]),
                "capabilities": caps,
                "retry_after": retry_after,
                "message": message,
            }
        )

    @router.get("/backups", response_class=HTMLResponse)
    def backups_page(request: Request):
        sid = guard(request)
        home = _home()
        bdir = home / "backups"
        rows = "".join(
            "<tr><td class=ltr>" + f.name + "</td><td>" + f"{f.stat().st_size:,}" + "</td></tr>"
            for f in sorted(
                bdir.glob("zero-backup-*"), key=lambda x: x.stat().st_mtime, reverse=True
            )
        )
        rows = rows or "<tr><td colspan=2 class=muted>no archives</td></tr>"
        sched = "-"
        state = ""
        if cfgsvc_exists():
            sched = cfg_load().backups.schedule
        sp = bdir / "last-backup.json"
        if sp.exists():
            state = sp.read_text(encoding="utf-8")[:300]
        body = (
            "<h3>Backups - schedule: " + sched + "</h3>"
            "<table><tr><th>archive</th><th>bytes</th></tr>"
            + rows
            + "</table><pre class=ltr>"
            + state
            + "</pre>"
            + '<form method="post" action="/admin/backups/run-now">'
            + '<input type="hidden" name="csrf" value="'
            + _csrf(sid)
            + '">'
            + "<button>Run backup now</button></form>"
        )
        return _page(body, sid)

    @router.post("/backups/run-now")
    def backups_run_now(request: Request, csrf: str = Form("")):
        sid = guard(request)
        if not _check_csrf(sid, csrf):
            return HTMLResponse("bad csrf", status_code=400)
        daemon = getattr(app.state, "backup_daemon", None)
        if daemon is None:
            import urllib.parse as up

            return RedirectResponse(
                "/admin/backups?msg="
                + up.quote("backup daemon disabled (schedule off / test env)"),
                status_code=303,
            )
        res = daemon.run_once(force=True)
        import urllib.parse as up

        flag = "ok" if res.get("ok") else "failed"
        return RedirectResponse("/admin/backups?msg=" + up.quote(flag), status_code=303)

    @router.get("/usage", response_class=HTMLResponse)
    def usage_page(request: Request):
        sid = guard(request)
        summary = _usage_summary()
        rows = "".join(
            "<tr><td class=ltr>"
            + str(r.get("day"))
            + "</td>"
            + "<td class=ltr>"
            + str(r.get("provider"))
            + "</td>"
            + "<td class=ltr>"
            + str(r.get("model"))
            + "</td>"
            + "<td>"
            + str(r.get("requests"))
            + "</td>"
            + "<td>"
            + str(r.get("it"))
            + "</td>"
            + "<td>"
            + str(r.get("ot"))
            + "</td>"
            + "<td>$"
            + str(r.get("cost"))
            + "</td></tr>"
            for r in summary
        )
        rows = rows or ("<tr><td colspan=7 class=muted>no usage recorded yet</td></tr>")
        body = (
            "<h3>Usage <span class=muted>(estimates; last 30 days)"
            "</span></h3>" + "<table><tr><th>day</th><th>provider</th><th>model</th>"
            "<th>req</th><th>in tok</th><th>out tok</th>"
            "<th>est cost</th></tr>" + rows + "</table>"
        )
        return _page(body, sid)

    # ------------------------------------------------------------------
    # Interactive chat + execution streaming (GAPs 5 & 6)
    # ------------------------------------------------------------------

    def _chat_service():
        import os as _os

        svc = getattr(app.state, "chat_service", None)
        if svc is not None:
            return svc
        if _svc is None:
            raise RuntimeError("services not wired")
        from zero.app.chat_service import ChatService, TokenBucketRateLimiter

        try:
            rate_per_min = int(
                _os.environ.get("ZERO_CHAT_RATE_LIMIT_PER_MIN", "10").strip() or "10"
            )
        except ValueError:
            rate_per_min = 10
        rate_per_min = max(1, min(rate_per_min, 120))
        svc = ChatService(
            providers=_svc.providers,
            authorization=_svc.authorization,
            tools=getattr(_svc, "tools", None),
            rate_limiter=TokenBucketRateLimiter(rate_per_min),
        )
        app.state.chat_service = svc
        return svc

    def _chat_provider_model():
        """Pick the first registered provider/model pair."""
        settings = getattr(app.state, "settings", None)
        names = _svc.providers.registered_provider_names if _svc is not None else []
        if not names:
            raise RuntimeError("no provider adapter registered")
        primary = names[0]
        if primary == "anthropic":
            model_name = settings.anthropic_model if settings else "claude-sonnet-4"
        elif primary == "openai-compatible":
            model_name = settings.openai_model if settings else "gpt-4o-mini"
        else:
            model_name = "fake-standard"
        return primary, model_name

    @router.get("/chat", response_class=HTMLResponse)
    def chat_page(request: Request):
        sid = guard(request)
        body = """
<h3>Chat <span class=muted>(ephemeral single-turn; no plan/execution)</span></h3>
<form method="post" action="/admin/chat">
<input type="hidden" name="csrf" value="{csrf}">
<textarea name="message" rows="4" style="width:100%%" required></textarea><br>
<button>Send</button></form>
<div class=card id=chatout>{result}</div>"""
        result_html = ""
        msg = request.query_params.get("resp")
        if msg:
            import urllib.parse as up

            result_html = "<pre class=ltr>" + up.unquote(msg)[:4000] + "</pre>"
        body = (
            body.replace("{csrf}", _csrf(sid)).replace("{result}", result_html).replace("%%", "%")
        )
        return _page(body, sid)

    @router.post("/chat")
    async def chat_submit(request: Request):
        sid_raw = request.cookies.get("zero_admin") or ""
        form = await request.form()
        csrf = str(form.get("csrf", ""))
        if not _valid_session(request) or not _check_csrf(sid_raw, csrf):
            return HTMLResponse("bad session/csrf", status_code=400)
        message = str(form.get("message", "")).strip()
        import urllib.parse as up

        if not message:
            return RedirectResponse("/admin/chat", status_code=303)
        try:
            project = _ensure_project(_svc)
            provider_name, model_name = _chat_provider_model()
            turn = _chat_service().complete(
                project_id=project.id,
                actor_id=project.owner_user_id,
                message=message,
                provider=provider_name,
                model_name=model_name,
                source="web",
            )
            payload = {
                "content": turn.content,
                "tool_calls_executed": list(turn.tool_calls_executed),
                "usage": turn.usage,
                "provider_request_id": turn.provider_request_id,
            }
            return RedirectResponse(
                "/admin/chat?resp=" + up.quote(json.dumps(payload)[:3800]), status_code=303
            )
        except Exception as exc:  # noqa: BLE001 - surface typed failures in UI
            return RedirectResponse(
                "/admin/chat?resp=" + up.quote(f"error: {type(exc).__name__}"), status_code=303
            )

    @router.post("/chat/{project_id}")
    async def chat_api(project_id: str, request: Request):
        """JSON chat endpoint (GAP 6): one ephemeral completion."""
        guard(request)
        body = await request.json()
        message = str(body.get("message", "")).strip()
        if not message:
            return JSONResponse({"detail": "message must not be empty"}, status_code=422)
        agent_scope = str(body.get("agent_scope") or "main_worker")
        try:
            max_tool_rounds = int(body.get("max_tool_rounds", 3))
        except (TypeError, ValueError):
            return JSONResponse({"detail": "max_tool_rounds must be int"}, status_code=422)
        project = next(
            (
                p
                for p in _svc.identity.list_projects()
                if p.id.value == project_id or p.name == project_id
            ),
            None,
        )
        if project is None:
            return JSONResponse({"detail": "unknown project"}, status_code=404)
        try:
            provider_name, model_name = _chat_provider_model()
            turn = _chat_service().complete(
                project_id=project.id,
                actor_id=project.owner_user_id,
                message=message,
                agent_scope=agent_scope,
                max_tool_rounds=max_tool_rounds,
                provider=provider_name,
                model_name=model_name,
                source="web",
            )
        except Exception as exc:  # noqa: BLE001 - typed failures surface in the response
            name = type(exc).__name__
            if name == "ChatRateLimitError":
                return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
            return JSONResponse({"detail": f"{name}"}, status_code=502)
        return JSONResponse(
            {
                "content": turn.content,
                "tool_calls_executed": list(turn.tool_calls_executed),
                "usage": turn.usage,
                "provider_request_id": turn.provider_request_id,
            }
        )

    @router.get("/executions/{execution_id}/stream")
    def execution_stream(execution_id: str, request: Request):
        """SSE stream of runtime events for one execution (GAP 5)."""
        guard(request)
        if _svc is None:
            return JSONResponse({"detail": "services not wired"}, status_code=503)
        conn = _svc.database.connect()
        row = conn.execute("SELECT id FROM executions WHERE id = ?", (execution_id,)).fetchone()
        if row is None:
            return JSONResponse({"detail": "unknown execution"}, status_code=404)
        try:
            q = _stream_hub(app).subscribe(execution_id)
        except LookupError:
            return JSONResponse({"detail": "too many subscribers"}, status_code=429)

        import queue as _queue

        def generate():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        payload = q.get(timeout=15.0)
                    except _queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                    if payload.get("type") == "done":
                        break
            finally:
                _stream_hub(app).unsubscribe(execution_id, q)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.include_router(router)
