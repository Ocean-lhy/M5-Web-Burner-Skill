#!/usr/bin/env python3
"""webburner - M5Stack Web Burner (burner.m5stack.com) firmware sharing helper.

Stdlib only (Python 3.8+). Auth = the same Bearer token the website uses
(localStorage key ``m5burner.accessToken``).

Read-only commands never change anything on the server. Every mutating command
prints the resolved plan first, supports ``--dry-run`` and asks for ``yes``
(or takes ``--yes``) before sending the request.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import mimetypes
import os
import platform
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "0.3"
DEFAULT_BASE_URL = "https://burner.m5stack.com"
BEARER_ENV = "WEB_BURNER_BEARER_TOKEN"
BEARER_FILE_ENV = "WEB_BURNER_BEARER_FILE"
NO_BROWSER_ENV = "WEB_BURNER_NO_BROWSER"
LS_KEY = b"m5burner.accessToken"
# A JWT header is base64url of '{"<letter>...', so it always starts with the
# same 3 characters; derive them instead of hard-coding.
JWT_HEAD = base64.urlsafe_b64encode(b'{"a')[:3]
_SEG = rb"[A-Za-z0-9_\-]"
JWT_NEAR_KEY_RE = re.compile(
    rb"m5burner\.accessToken.{0,40}?(" + re.escape(JWT_HEAD) + _SEG + rb"+=*\." + _SEG + rb"+=*\." + _SEG + rb"*)",
    re.S,
)
JWT_LOOSE_RE = re.compile(
    rb"(" + re.escape(JWT_HEAD) + _SEG + rb"{10,}\." + _SEG + rb"{10,}\." + _SEG + rb"{10,})"
)
VISIBILITIES = ("PUBLIC", "PRIVATE")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


class WBError(RuntimeError):
    pass


def base_url() -> str:
    return os.environ.get("WEB_BURNER_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def api_url(path: str) -> str:
    return base_url() + "/api/v1" + path


# --------------------------------------------------------------------------- auth

def bearer_file() -> Path:
    override = os.environ.get(BEARER_FILE_ENV)
    return Path(override).expanduser() if override else Path.home() / ".web_burner_bearer"


def jwt_payload(token: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    seg = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(seg.encode("ascii")))
    except Exception:
        return {}


def jwt_exp(token: str) -> Optional[int]:
    exp = jwt_payload(token).get("exp")
    try:
        return int(exp) if exp is not None else None
    except (TypeError, ValueError):
        return None


def jwt_usable(token: Optional[str], skew: int = 60) -> bool:
    if not token or not token.startswith(JWT_HEAD.decode()) or token.count(".") != 2:
        return False
    exp = jwt_exp(token)
    return exp is None or exp > time.time() + skew


def describe_expiry(token: str) -> str:
    exp = jwt_exp(token)
    if exp is None:
        return "token 不带过期时间（服务器端会话，失效时接口返回 401）"
    left = exp - int(time.time())
    when = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(exp))
    return f"expires {when} ({'expired' if left <= 0 else f'{left // 3600}h{(left % 3600) // 60}m left'})"


def read_bearer_file() -> Optional[str]:
    p = bearer_file()
    try:
        tok = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return tok or None


def write_bearer_file(token: str) -> Path:
    p = bearer_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(p), flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token.strip() + "\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


BROWSERS = ("edge", "chrome", "chromium", "brave")


def browser_roots(browser: str) -> List[Path]:
    home = Path.home()
    system = platform.system()
    roots: List[Path] = []
    if system == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        table = {
            "edge": [local / "Microsoft" / "Edge" / "User Data"],
            "chrome": [local / "Google" / "Chrome" / "User Data"],
            "chromium": [local / "Chromium" / "User Data"],
            "brave": [local / "BraveSoftware" / "Brave-Browser" / "User Data"],
        }
    elif system == "Darwin":
        sup = home / "Library" / "Application Support"
        table = {
            "edge": [sup / "Microsoft Edge"],
            "chrome": [sup / "Google" / "Chrome"],
            "chromium": [sup / "Chromium"],
            "brave": [sup / "BraveSoftware" / "Brave-Browser"],
        }
    else:
        cfg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        table = {
            "edge": [cfg / "microsoft-edge", cfg / "microsoft-edge-beta", cfg / "microsoft-edge-dev"],
            "chrome": [cfg / "google-chrome", cfg / "google-chrome-beta"],
            "chromium": [cfg / "chromium", home / "snap" / "chromium" / "common" / "chromium"],
            "brave": [cfg / "BraveSoftware" / "Brave-Browser"],
        }
    for b in (BROWSERS if browser == "all" else (browser,)):
        roots.extend(table.get(b, []))
    return [r for r in roots if r.is_dir()]


def leveldb_dirs(browser: str = "all", profile: Optional[str] = None) -> List[Path]:
    out: List[Path] = []
    for root in browser_roots(browser):
        if profile:
            profiles = [root / profile]
        else:
            profiles = [root / "Default"] + sorted(root.glob("Profile *"))
        for prof in profiles:
            d = prof / "Local Storage" / "leveldb"
            if d.is_dir():
                out.append(d)
    return out


def _scan(data: bytes) -> Optional[str]:
    m = JWT_NEAR_KEY_RE.search(data)
    if m:
        return m.group(1).decode("ascii", "ignore")
    idx = data.find(LS_KEY)
    if idx >= 0:
        m = JWT_LOOSE_RE.search(data[max(0, idx - 32): idx + 800])
        if m:
            return m.group(1).decode("ascii", "ignore")
    return None


def extract_from_browser(browser: str = "all", profile: Optional[str] = None) -> Tuple[Optional[str], List[str]]:
    """Return (newest usable token, scanned dirs). Files are copied aside because
    a running browser may lock them. Nothing is written back to the browser."""
    best: Optional[Tuple[int, str]] = None
    scanned: List[str] = []
    for d in leveldb_dirs(browser, profile):
        scanned.append(str(d))
        with tempfile.TemporaryDirectory(prefix="webburner-ls-") as tmp:
            for item in d.iterdir():
                if item.suffix.lower() in (".ldb", ".log"):
                    try:
                        shutil.copy2(item, Path(tmp) / item.name)
                    except OSError:
                        continue
            for f in sorted(Path(tmp).iterdir()):
                try:
                    tok = _scan(f.read_bytes())
                except OSError:
                    continue
                if not tok or not jwt_usable(tok, skew=0):
                    continue
                p = jwt_payload(tok)
                exp = int(p.get("exp") or p.get("iat") or 0)
                if best is None or exp >= best[0]:
                    best = (exp, tok)
    return (best[1] if best else None), scanned


def resolve_bearer(explicit: Optional[str] = None, allow_browser: bool = True) -> str:
    candidates = [explicit, os.environ.get(BEARER_ENV), read_bearer_file()]
    for tok in candidates:
        if tok and jwt_usable(tok.strip()):
            return tok.strip()
    if allow_browser and os.environ.get(NO_BROWSER_ENV, "") not in ("1", "true", "yes"):
        tok, _ = extract_from_browser()
        if tok:
            write_bearer_file(tok)
            return tok
    raise WBError(
        "未找到可用的登录凭证 / no usable Bearer token.\n"
        "  先在浏览器登录 https://burner.m5stack.com ，然后运行: webburner.py login\n"
        "  或手动粘贴: webburner.py login --paste  (DevTools > Application > Local Storage > m5burner.accessToken)"
    )


# --------------------------------------------------------------------------- http

def _encode_multipart(fields: List[Tuple[str, str]], files: List[Tuple[str, Path]]) -> Tuple[bytes, str]:
    boundary = "----webburner" + uuid.uuid4().hex
    buf: List[bytes] = []
    for name, value in fields:
        buf.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode("utf-8"))
        buf.append(str(value).encode("utf-8") + b"\r\n")
    for name, path in files:
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        fname = path.name.replace('"', "_")
        buf.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{fname}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n".encode("utf-8")
        )
        buf.append(path.read_bytes() + b"\r\n")
    buf.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(buf), f"multipart/form-data; boundary={boundary}"


def request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    token: Optional[str] = None,
    json_body: Any = None,
    fields: Optional[List[Tuple[str, str]]] = None,
    files: Optional[List[Tuple[str, Path]]] = None,
    timeout: int = 60,
) -> Dict[str, Any]:
    url = api_url(path)
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    headers = {"Accept": "application/json", "User-Agent": f"webburner-skill/{VERSION}",
               "X-Request-Id": str(uuid.uuid4())}
    data = None
    if token:
        headers["Authorization"] = "Bearer " + token
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif fields is not None or files:
        data, headers["Content-Type"] = _encode_multipart(fields or [], files or [])
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    except urllib.error.URLError as e:
        raise WBError(f"网络错误 / network error: {e.reason}")
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        body = {"raw": raw[:300].decode("utf-8", "replace")}
    if not isinstance(body, dict):
        body = {"data": body}
    code = body.get("code")
    if status >= 400 or (isinstance(code, int) and code >= 400):
        msg = body.get("msg") or body.get("message") or body.get("raw") or ""
        err = (body.get("data") or {}).get("errorCode") if isinstance(body.get("data"), dict) else None
        hint = ""
        if status == 401 or code == 401:
            hint = "\n  Bearer 已失效？在浏览器重新登录后运行: webburner.py login"
        raise WBError(f"HTTP {status} {method} {path}: {msg}" + (f" [{err}]" if err else "") + hint)
    body["_http_status"] = status
    return body


def get(path: str, **kw: Any) -> Dict[str, Any]:
    return request("GET", path, **kw)


def data_of(body: Dict[str, Any]) -> Any:
    return body.get("data")


def paged_all(path: str, token: Optional[str], params: Dict[str, Any], page_size: int = 100) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    page = 1
    while True:
        p = dict(params, pageNum=page, pageSize=page_size)
        body = get(path, params=p, token=token)
        chunk = body.get("rows")
        if chunk is None and isinstance(body.get("data"), dict):
            chunk = body["data"].get("rows")
        chunk = chunk or []
        rows.extend(chunk)
        total_raw = body.get("total")
        if total_raw is None and isinstance(body.get("data"), dict):
            total_raw = body["data"].get("total")
        total = int(total_raw or 0)
        if not chunk or page * page_size >= total:
            return rows
        page += 1


# --------------------------------------------------------------------------- helpers

def wrap_description(text: Optional[str]) -> Optional[str]:
    """Web Burner stores descriptions as JSON {"format":"markdown","content":...}."""
    if text is None:
        return None
    s = text.strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and "content" in obj and "format" in obj:
            return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except ValueError:
        pass
    return json.dumps({"format": "markdown", "content": text}, ensure_ascii=False, separators=(",", ":"))


def unwrap_description(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, dict):
        return str(value.get("content", ""))
    try:
        obj = json.loads(value)
        if isinstance(obj, dict) and "content" in obj:
            return str(obj["content"])
    except (TypeError, ValueError):
        pass
    return str(value)


def read_text_arg(inline: Optional[str], file: Optional[str]) -> Optional[str]:
    if inline is not None and file:
        raise WBError("--xxx 和 --xxx-file 只能二选一 / give text or file, not both")
    if file:
        return Path(file).expanduser().read_text(encoding="utf-8")
    return inline


def names(items: Any, key: str = "name") -> str:
    if not items:
        return "-"
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append(str(it.get(key) or it.get("categoryName") or it.get("deviceName") or it.get("slug") or it.get("id")))
        else:
            out.append(str(it))
    return ", ".join(out)


def ids(items: Any, *keys: str) -> List[str]:
    out = []
    for it in items or []:
        if isinstance(it, dict):
            for k in keys + ("id",):
                if it.get(k) is not None:
                    out.append(str(it[k]))
                    break
        else:
            out.append(str(it))
    return out


def jdump(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def check_file(path: Optional[str], what: str, required: bool = False) -> Optional[Path]:
    if not path:
        if required:
            raise WBError(f"缺少 {what}")
        return None
    p = Path(path).expanduser()
    if not p.is_file():
        raise WBError(f"{what} 不存在: {p}")
    return p


def file_info(p: Optional[Path]) -> str:
    if not p:
        return "(unchanged / 不修改)"
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    return f"{p} ({p.stat().st_size} bytes, sha256 {h[:12]}…)"


def confirm(plan: List[Tuple[str, Any]], title: str, args: argparse.Namespace) -> bool:
    print(f"\n=== {title} ===")
    print(f"target: {base_url()}")
    width = max(len(k) for k, _ in plan) if plan else 0
    for k, v in plan:
        text = str(v)
        if "\n" in text or len(text) > 160:
            text = text.replace("\n", " ")[:160] + "…"
        print(f"  {k.ljust(width)} : {text}")
    if args.dry_run:
        print("\n[dry-run] 未发送任何修改请求 / nothing was sent.")
        return False
    if args.yes:
        return True
    try:
        ans = input("\n确认执行? 输入 yes 继续 / type 'yes' to continue: ").strip().lower()
    except EOFError:
        ans = ""
    if ans != "yes":
        print("已取消 / cancelled.")
        return False
    return True


# --------------------------------------------------------------------------- domain

def get_catalog(kind: str) -> List[Dict[str, Any]]:
    path = "/firmware-categories" if kind == "category" else "/devices"
    d = data_of(get(path))
    if isinstance(d, dict):
        d = d.get("rows") or d.get("list") or d.get("categories") or d.get("devices") or []
    return d or []


_ALIASES: Dict[str, str] = {}


def catalog_maps() -> Tuple[Dict[str, str], Dict[str, str]]:
    """id -> display name; also fills _ALIASES (lower-case name/code/slug -> id)."""
    def m(kind: str) -> Dict[str, str]:
        out = {}
        for it in get_catalog(kind):
            i = str(it.get("categoryId") or it.get("deviceId") or it.get("id"))
            out[i] = str(it.get("name") or it.get("categoryName") or it.get("deviceName") or it.get("deviceSlug"))
            for k in ("categoryName", "categoryCode", "deviceName", "deviceCode", "deviceSlug", "name"):
                if it.get(k):
                    _ALIASES[f"{kind}:{str(it[k]).lower()}"] = i
        return out
    return m("category"), m("device")


def get_project(fid: str, token: Optional[str]) -> Dict[str, Any]:
    d = data_of(get(f"/firmwares/{urllib.parse.quote(str(fid))}", token=token))
    if not isinstance(d, dict):
        raise WBError(f"固件项目不存在: {fid}")
    return d


def get_versions(fid: str, token: Optional[str]) -> List[Dict[str, Any]]:
    params = {"scope": "ALL"} if token else None
    d = data_of(get(f"/firmwares/{urllib.parse.quote(str(fid))}/versions", token=token, params=params))
    if isinstance(d, dict):
        d = d.get("rows") or d.get("versions") or []
    return d or []


def get_edit_detail(fid: str, vid: str, token: str) -> Dict[str, Any]:
    d = data_of(get(f"/firmwares/{fid}/versions/{vid}", token=token))
    return d if isinstance(d, dict) else {}


def my_projects(token: str) -> List[Dict[str, Any]]:
    return paged_all("/users/me/firmware-projects", token, {"visibility": "ALL"})


def my_versions(token: str) -> List[Dict[str, Any]]:
    return paged_all("/users/me/firmwares", token, {"visibility": "ALL"})


def find_version(versions: List[Dict[str, Any]], name: Optional[str], vid: Optional[str]) -> Dict[str, Any]:
    if vid:
        hit = [v for v in versions if str(v.get("versionId")) == str(vid)]
    else:
        hit = [v for v in versions if v.get("versionName") == name]
    if len(hit) != 1:
        avail = ", ".join(f"{v.get('versionName')}({v.get('versionId')})" for v in versions) or "-"
        raise WBError(f"找不到唯一匹配的版本 {vid or name!r}；现有版本: {avail}")
    return hit[0]


def owned_project(token: str, fid: str) -> Dict[str, Any]:
    for p in my_projects(token):
        if str(p.get("firmwareId")) == str(fid):
            return p
    raise WBError(f"firmwareId {fid} 不在你的账号下 / not one of your projects (see: webburner.py mine)")


# --------------------------------------------------------------------------- printers

def print_rows(rows: List[Dict[str, Any]], cols: List[Tuple[str, str]]) -> None:
    if not rows:
        print("(empty)")
        return
    table = [[h for h, _ in cols]] + [[str(r.get(k) if r.get(k) is not None else "-") for _, k in cols] for r in rows]
    widths = [min(48, max(len(row[i]) for row in table)) for i in range(len(cols))]
    for n, row in enumerate(table):
        print("  ".join(c[:48].ljust(widths[i]) for i, c in enumerate(row)))
        if n == 0:
            print("  ".join("-" * w for w in widths))


def print_project(p: Dict[str, Any]) -> None:
    stats = p.get("statistics") or {}
    cur = p.get("currentVersion") or {}
    print(f"firmwareId : {p.get('firmwareId')}")
    print(f"name       : {p.get('firmwareName')}")
    print(f"author     : {(p.get('developer') or {}).get('nickname') or '-'}")
    print(f"categories : {names(p.get('categories'))}")
    print(f"devices    : {names(p.get('supportedDevices'))}")
    print(f"sourceUrl  : {p.get('sourceUrl') or '-'}")
    print(f"current    : {cur.get('versionName') or p.get('versionName') or '-'} ({cur.get('versionId') or p.get('versionId') or '-'})")
    print(f"downloads  : {stats.get('downloadCount', '-')}   likes: {stats.get('likeCount', p.get('likeCount', '-'))}   comments: {stats.get('commentCount', '-')}")
    print(f"page       : {base_url()}/firmware/{p.get('firmwareId')}")
    desc = unwrap_description(p.get("firmwareDescription"))
    if desc:
        print("\n--- description ---\n" + desc.strip())


# --------------------------------------------------------------------------- commands

def cmd_login(a: argparse.Namespace) -> int:
    tok: Optional[str] = None
    if a.token:
        tok = a.token.strip()
    elif a.paste:
        tok = getpass.getpass("粘贴 m5burner.accessToken (输入不回显) / paste token: ").strip().strip('"')
    elif a.email:
        pwd = os.environ.get("WEB_BURNER_PASSWORD") or getpass.getpass("Web Burner 密码 / password: ")
        body = request("POST", "/auth/login", json_body={
            "email": a.email.strip().lower(), "password": pwd, "rememberMe": True,
            "deviceName": f"webburner-skill on {platform.system()}"})
        d = data_of(body) or {}
        tok = d.get("accessToken") if isinstance(d, dict) else None
        if not tok:
            raise WBError("登录接口未返回 accessToken / login returned no token")
    else:
        tok, scanned = extract_from_browser(a.browser, a.profile)
        if not tok:
            print("扫描过的浏览器目录 / scanned:")
            for s in scanned or ["(none found)"]:
                print("  " + s)
            raise WBError(
                "没在浏览器里找到有效的 m5burner.accessToken。\n"
                "  1) 用 Edge/Chrome 打开 https://burner.m5stack.com 并登录；或\n"
                "  2) DevTools(F12) > Application > Local Storage > https://burner.m5stack.com > m5burner.accessToken，"
                "复制值后运行: webburner.py login --paste")
    if not jwt_usable(tok):
        raise WBError("token 格式不对或已过期 / token invalid or expired")
    me = data_of(get("/users/me", token=tok)) or {}
    path = write_bearer_file(tok)
    print(f"登录成功 / logged in as: {me.get('nickname') or me.get('username')} <{me.get('email')}> userId={me.get('userId')}")
    print(f"token saved to {path} (0600), {describe_expiry(tok)}")
    return 0


def cmd_logout(a: argparse.Namespace) -> int:
    p = bearer_file()
    if p.exists():
        p.unlink()
        print(f"已删除 / removed {p}")
    else:
        print(f"没有保存的 token / nothing at {p}")
    if os.environ.get(BEARER_ENV):
        print(f"注意: 环境变量 {BEARER_ENV} 仍然存在，请自行清除。")
    return 0


def cmd_me(a: argparse.Namespace) -> int:
    tok = resolve_bearer(a.token)
    me = data_of(get("/users/me", token=tok)) or {}
    if a.json:
        jdump(me)
        return 0
    for k in ("userId", "username", "nickname", "email", "roleKey", "status", "registeredAt"):
        print(f"{k:12}: {me.get(k)}")
    print(f"{'token':12}: {describe_expiry(tok)}")
    return 0


def cmd_summary(a: argparse.Namespace) -> int:
    tok = resolve_bearer(a.token)
    s = data_of(get("/users/me/developer-summary", token=tok)) or {}
    if a.json:
        jdump(s)
        return 0
    print(f"固件数 firmware : {s.get('totalFirmware')}")
    print(f"下载 downloads  : {s.get('totalDownloads')}")
    print(f"点赞 likes      : {s.get('totalLikes')}")
    print(f"关注 followers  : {s.get('totalFollowers')}")
    print(f"积分 points     : {s.get('totalPoints')}")
    return 0


def cmd_stats(a: argparse.Namespace) -> int:
    tok = resolve_bearer(a.token)
    summary = data_of(get("/users/me/developer-summary", token=tok)) or {}
    projects = my_projects(tok)
    if a.firmware_id:
        projects = [p for p in projects if str(p.get("firmwareId")) == str(a.firmware_id)]
        if not projects:
            raise WBError(f"firmwareId {a.firmware_id} 不在你的账号下")
    report = {"summary": summary, "projects": []}
    for p in projects:
        fid = str(p.get("firmwareId"))
        try:
            detail = get_project(fid, tok)
        except WBError:
            detail = {}
        stats = detail.get("statistics") or {}
        vers = get_versions(fid, tok) if not a.no_versions else []
        report["projects"].append({
            "firmwareId": fid, "firmwareName": p.get("firmwareName"),
            "visibility": p.get("visibility"), "status": p.get("status"),
            "latestVersion": p.get("versionName"), "reviewReason": p.get("reviewReason"),
            "projectRevisionStatus": p.get("projectRevisionStatus"),
            "downloads": stats.get("downloadCount"), "likes": stats.get("likeCount", p.get("likeCount")),
            "comments": stats.get("commentCount"),
            "versions": [{k: v.get(k) for k in ("versionId", "versionName", "status", "visibility", "current", "downloadCount", "uploadedAt", "reviewReason")} for v in vers],
        })
    if a.json:
        jdump(report)
        return 0
    s = summary
    print(f"总计 / totals: firmware={s.get('totalFirmware')} downloads={s.get('totalDownloads')} likes={s.get('totalLikes')} followers={s.get('totalFollowers')} points={s.get('totalPoints')}\n")
    for p in report["projects"]:
        rev = p["projectRevisionStatus"]
        print(f"■ {p['firmwareName']}  [{p['firmwareId']}]")
        print(f"  {p['visibility']}/{p['status']}  latest={p['latestVersion']}  downloads={p['downloads']}  likes={p['likes']}  comments={p['comments']}"
              + (f"  项目信息审核={rev}" if rev else "") + (f"  reason={p['reviewReason']}" if p['reviewReason'] else ""))
        for v in p["versions"]:
            print(f"    - {v['versionName']:<14} {str(v['status']):<11} {str(v['visibility']):<8} dl={v['downloadCount']}"
                  + ("  (current)" if v.get("current") else "") + f"  id={v['versionId']}"
                  + (f"  reason={v['reviewReason']}" if v.get("reviewReason") else ""))
        print()
    return 0


def cmd_mine(a: argparse.Namespace) -> int:
    tok = resolve_bearer(a.token)
    rows = my_projects(tok)
    if a.json:
        jdump(rows)
        return 0
    print_rows(rows, [("firmwareId", "firmwareId"), ("name", "firmwareName"), ("latest", "versionName"),
                      ("visibility", "visibility"), ("status", "status"), ("likes", "likeCount"),
                      ("projectReview", "projectRevisionStatus"), ("uploadedAt", "uploadedAt")])
    return 0


def cmd_my_versions(a: argparse.Namespace) -> int:
    tok = resolve_bearer(a.token)
    rows = my_versions(tok)
    if a.firmware_id:
        rows = [r for r in rows if str(r.get("firmwareId")) == str(a.firmware_id)]
    if a.json:
        jdump(rows)
        return 0
    print_rows(rows, [("firmwareId", "firmwareId"), ("name", "firmwareName"), ("version", "versionName"),
                      ("versionId", "versionId"), ("visibility", "visibility"), ("status", "status"),
                      ("uploadedAt", "uploadedAt")])
    return 0


def cmd_resolve_mine(a: argparse.Namespace) -> int:
    tok = resolve_bearer(a.token)
    rows = my_projects(tok)
    exact = [r for r in rows if r.get("firmwareName") == a.name]
    fuzzy = [r for r in rows if a.name.lower() in str(r.get("firmwareName", "")).lower() and r not in exact]
    if exact:
        print("精确匹配 / exact:")
        print_rows(exact, [("firmwareId", "firmwareId"), ("name", "firmwareName"), ("latest", "versionName"), ("status", "status")])
    if fuzzy:
        print("\n相似 / similar:")
        print_rows(fuzzy, [("firmwareId", "firmwareId"), ("name", "firmwareName"), ("latest", "versionName"), ("status", "status")])
    if not exact and not fuzzy:
        print("没有匹配的项目 / no match")
        return 1
    return 0


def cmd_search(a: argparse.Namespace) -> int:
    body = get("/firmwares", params={"keyword": a.keyword, "sort": a.sort, "pageNum": a.page, "pageSize": a.size})
    rows = body.get("rows") or []
    if a.json:
        jdump(body)
        return 0
    print(f"total={body.get('total')}")
    for r in rows:
        r["_author"] = (r.get("developer") or {}).get("nickname")
        r["_ver"] = (r.get("currentVersion") or {}).get("versionName")
        r["_dl"] = (r.get("statistics") or {}).get("downloadCount", r.get("downloadCount"))
    print_rows(rows, [("firmwareId", "firmwareId"), ("name", "firmwareName"), ("version", "_ver"),
                      ("author", "_author"), ("downloads", "_dl")])
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    tok = None
    try:
        tok = resolve_bearer(a.token, allow_browser=False)
    except WBError:
        pass
    p = get_project(a.firmware_id, tok)
    if a.json:
        jdump(p)
    else:
        print_project(p)
    return 0


def cmd_versions(a: argparse.Namespace) -> int:
    tok = None
    try:
        tok = resolve_bearer(a.token, allow_browser=False)
    except WBError:
        pass
    vers = get_versions(a.firmware_id, tok)
    if a.json:
        jdump(vers)
        return 0
    print(f"scope={'ALL (Bearer)' if tok else 'PUBLIC'}")
    for v in vers:
        v["_devs"] = names(v.get("supportedDevices"))
    print_rows(vers, [("versionId", "versionId"), ("version", "versionName"), ("status", "status"),
                      ("visibility", "visibility"), ("current", "current"), ("downloads", "downloadCount"),
                      ("bin", "binFileName"), ("devices", "_devs")])
    return 0


def cmd_edit_detail(a: argparse.Namespace) -> int:
    tok = resolve_bearer(a.token)
    vid = a.version_id or find_version(get_versions(a.firmware_id, tok), a.version, None).get("versionId")
    d = get_edit_detail(a.firmware_id, str(vid), tok)
    if a.json:
        jdump(d)
        return 0
    for k in ("firmwareId", "firmwareName", "versionId", "versionName", "visibility", "status", "sourceUrl",
              "coverUrl", "binFileName", "projectRevisionId", "projectRevisionStatus", "reviewReason"):
        if k in d:
            print(f"{k:22}: {d.get(k)}")
    print(f"{'categories':22}: {names(d.get('categories'))}")
    print(f"{'devices':22}: {names(d.get('supportedDevices'))}")
    print("\n--- versionDescription ---\n" + unwrap_description(d.get("versionDescription")).strip())
    return 0


def cmd_catalog(a: argparse.Namespace) -> int:
    kind = "category" if a.command == "categories" else "device"
    rows = get_catalog(kind)
    if a.json:
        jdump(rows)
        return 0
    out = []
    for it in rows:
        out.append({"id": it.get("categoryId") or it.get("deviceId") or it.get("id"),
                    "name": it.get("name") or it.get("categoryName") or it.get("deviceName"),
                    "code": it.get("deviceSlug") or it.get("categoryCode") or "-",
                    "firmwareCount": it.get("firmwareCount")})
    print_rows(out, [("id", "id"), ("name", "name"), ("code", "code"), ("firmwares", "firmwareCount")])
    return 0


def cmd_download(a: argparse.Namespace) -> int:
    tok = None
    try:
        tok = resolve_bearer(a.token, allow_browser=False)
    except WBError:
        pass
    v = find_version(get_versions(a.firmware_id, tok), a.version, a.version_id)
    fname = Path(str(v.get("binFileName") or f"{a.firmware_id}-{v.get('versionName')}.bin")).name
    out_dir = Path(a.output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / fname
    if dest.exists() and not a.force:
        raise WBError(f"文件已存在 / exists: {dest} (use --force)")
    url = api_url(f"/firmwares/{a.firmware_id}/versions/{v.get('versionId')}/download")
    req = urllib.request.Request(url, headers={"User-Agent": f"webburner-skill/{VERSION}"})
    h = hashlib.sha256()
    n = 0
    with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            h.update(chunk)
            n += len(chunk)
    print(f"saved {dest}\nsize {n} bytes\nsha256 {h.hexdigest()}")
    return 0


# ---- mutations ----------------------------------------------------------------

def _resolve_catalog_ids(values: Optional[List[str]], mapping: Dict[str, str], kind: str) -> Optional[List[str]]:
    if not values:
        return None
    prefix = "category:" if kind == "分类" else "device:"
    rev = {k[len(prefix):]: v for k, v in _ALIASES.items() if k.startswith(prefix)}
    out = []
    for raw in values:
        for item in [x.strip() for x in raw.split(",") if x.strip()]:
            if item in mapping:
                out.append(item)
            elif item.lower() in rev:
                out.append(rev[item.lower()])
            else:
                raise WBError(f"未知{kind}: {item!r}，用 `webburner.py {'categories' if kind == '分类' else 'devices'}` 查看可用值")
    return out


def _visibility(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = v.upper()
    if v not in VISIBILITIES:
        raise WBError("--visibility 只能是 PUBLIC 或 PRIVATE")
    return v


def _vis_note(v: Optional[str]) -> str:
    return {"PUBLIC": "PUBLIC (需要官方审核，审核通过后所有人可见)",
            "PRIVATE": "PRIVATE (无需审核，仅自己可见)"}.get(v or "", str(v))


def _report(body: Dict[str, Any]) -> None:
    d = data_of(body)
    print(f"\n完成 / done: HTTP {body.get('_http_status')} {body.get('msg') or ''}")
    if isinstance(d, dict):
        for k in ("firmwareId", "versionId", "versionName", "status", "visibility", "projectRevisionId", "projectRevisionStatus", "code"):
            if k in d:
                print(f"  {k}: {d.get(k)}")
    print("提示: 用 `webburner.py versions <firmwareId>` / `stats` 复查。")


def cmd_create(a: argparse.Namespace) -> int:
    tok = resolve_bearer(a.token)
    bin_path = check_file(a.file, "--file 固件 bin", required=True)
    cover = check_file(a.cover, "--cover 封面图", required=True)
    vis = _visibility(a.visibility) or "PUBLIC"
    cats, devs = catalog_maps()
    cat_ids = _resolve_catalog_ids(a.category, cats, "分类") or []
    dev_ids = _resolve_catalog_ids(a.device, devs, "设备") or []
    if not cat_ids or not dev_ids:
        raise WBError("新项目必须至少指定一个 --category 和一个 --device")
    mine = [p for p in my_projects(tok) if p.get("firmwareName") == a.name]
    if mine:
        raise WBError(f"你已经有同名项目 {a.name!r} (firmwareId={mine[0].get('firmwareId')})。"
                      "服务器按名称匹配，再次上传会变成新增版本——请改用 add-version。")
    fdesc = wrap_description(read_text_arg(a.description, a.description_file) or "")
    vdesc = wrap_description(read_text_arg(a.version_description, a.version_description_file) or "")
    fields = [("firmwareName", a.name), ("versionName", a.version), ("sourceUrl", a.source_url or ""),
              ("categoryIds", json.dumps(cat_ids)), ("deviceIds", json.dumps(dev_ids)),
              ("firmwareDescription", fdesc), ("versionDescription", vdesc), ("visibility", vis)]
    plan = [("action", "POST /firmwares (新建项目 create project)"), ("firmwareName", a.name), ("versionName", a.version),
            ("categories", ", ".join(f"{i}:{cats.get(i)}" for i in cat_ids)),
            ("devices", ", ".join(f"{i}:{devs.get(i)}" for i in dev_ids)),
            ("visibility", _vis_note(vis)), ("sourceUrl", a.source_url or "-"),
            ("bin", file_info(bin_path)), ("cover", file_info(cover)),
            ("description", unwrap_description(fdesc) or "-"), ("versionDescription", unwrap_description(vdesc) or "-")]
    if not confirm(plan, "上传新固件 / create new firmware", a):
        return 0
    body = request("POST", "/firmwares", token=tok, fields=fields,
                   files=[("binFile", bin_path), ("coverFile", cover)], timeout=600)
    _report(body)
    return 0


def cmd_add_version(a: argparse.Namespace) -> int:
    tok = resolve_bearer(a.token)
    bin_path = check_file(a.file, "--file 固件 bin", required=True)
    cover = check_file(a.cover, "--cover 封面图")
    owned = owned_project(tok, a.firmware_id)
    name = owned.get("firmwareName")
    same_name = [p for p in my_projects(tok) if p.get("firmwareName") == name]
    if len(same_name) != 1:
        raise WBError(f"账号下有 {len(same_name)} 个同名项目 {name!r}，服务器按名称匹配，无法安全确定目标，已停止。")
    vers = get_versions(a.firmware_id, tok)
    dup = [v for v in vers if v.get("versionName") == a.version]
    if dup:
        raise WBError(f"版本 {a.version!r} 已存在 (versionId={dup[0].get('versionId')}, status={dup[0].get('status')})，"
                      "不会重复上传。要修改它请用 edit-version。")
    detail = get_project(a.firmware_id, tok)
    cats, devs = catalog_maps()
    cat_ids = _resolve_catalog_ids(a.category, cats, "分类") or ids(detail.get("categories") or owned.get("categories"), "categoryId")
    dev_ids = _resolve_catalog_ids(a.device, devs, "设备") or ids(detail.get("supportedDevices") or owned.get("supportedDevices"), "deviceId")
    source = a.source_url if a.source_url is not None else (detail.get("sourceUrl") or "")
    if a.source_url is None and not source:
        # project detail only shows the approved version; check newest versions too
        for v in sorted(vers, key=lambda x: str(x.get("uploadedAt") or x.get("versionId")), reverse=True)[:3]:
            source = get_edit_detail(a.firmware_id, str(v.get("versionId")), tok).get("sourceUrl") or ""
            if source:
                break
    vis = _visibility(a.visibility) or str(owned.get("visibility") or "PUBLIC")
    vdesc = wrap_description(read_text_arg(a.version_description, a.version_description_file) or "")
    fields = [("firmwareName", name), ("versionName", a.version), ("sourceUrl", source),
              ("categoryIds", json.dumps(cat_ids)), ("deviceIds", json.dumps(dev_ids)),
              ("versionDescription", vdesc), ("visibility", vis)]
    files = [("binFile", bin_path)] + ([("coverFile", cover)] if cover else [])
    plan = [("action", "POST /firmwares (给已有项目加新版本 add version)"),
            ("firmwareId", a.firmware_id), ("firmwareName", name),
            ("existing versions", ", ".join(str(v.get("versionName")) for v in vers) or "-"),
            ("new versionName", a.version), ("visibility", _vis_note(vis)), ("sourceUrl", source or "-"),
            ("categories", ", ".join(f"{i}:{cats.get(i)}" for i in cat_ids)),
            ("devices", ", ".join(f"{i}:{devs.get(i)}" for i in dev_ids)),
            ("bin", file_info(bin_path)), ("cover", file_info(cover) if cover else "(沿用旧封面 keep current)"),
            ("versionDescription", unwrap_description(vdesc) or "-")]
    if not confirm(plan, "上传新版本 / add version", a):
        return 0
    body = request("POST", "/firmwares", token=tok, fields=fields, files=files, timeout=600)
    _report(body)
    return 0


def cmd_edit_version(a: argparse.Namespace) -> int:
    tok = resolve_bearer(a.token)
    owned_project(tok, a.firmware_id)
    v = find_version(get_versions(a.firmware_id, tok), a.version, a.version_id)
    vid = str(v.get("versionId"))
    d = get_edit_detail(a.firmware_id, vid, tok)
    cover = check_file(a.cover, "--cover 封面图")
    new_desc_text = read_text_arg(a.version_description, a.version_description_file)
    vdesc = wrap_description(new_desc_text) if new_desc_text is not None else (d.get("versionDescription") or "")
    if vdesc and not str(vdesc).lstrip().startswith("{"):
        vdesc = wrap_description(str(vdesc))
    new_name = a.new_version_name or d.get("versionName") or v.get("versionName")
    source = a.source_url if a.source_url is not None else (d.get("sourceUrl") or "")
    vis = _visibility(a.visibility) or str(d.get("visibility") or v.get("visibility") or "PUBLIC")
    fields = [("versionName", new_name), ("versionDescription", vdesc), ("sourceUrl", source), ("visibility", vis)]
    plan = [("action", f"PUT /firmwares/{a.firmware_id}/versions/{vid}"),
            ("firmwareName", d.get("firmwareName")), ("version", f"{v.get('versionName')} -> {new_name}"),
            ("visibility", f"{d.get('visibility') or v.get('visibility')} -> {_vis_note(vis)}"),
            ("sourceUrl", f"{d.get('sourceUrl') or '-'} -> {source or '-'}"),
            ("cover", file_info(cover) if cover else "(不修改 keep)"),
            ("versionDescription", "(不修改 keep)" if new_desc_text is None else unwrap_description(vdesc))]
    if not confirm(plan, "修改版本 / edit version", a):
        return 0
    body = request("PUT", f"/firmwares/{a.firmware_id}/versions/{vid}", token=tok, fields=fields,
                   files=[("coverFile", cover)] if cover else [], timeout=300)
    _report(body)
    return 0


def cmd_edit_project(a: argparse.Namespace) -> int:
    tok = resolve_bearer(a.token)
    owned = owned_project(tok, a.firmware_id)
    detail = get_project(a.firmware_id, tok)
    cats, devs = catalog_maps()
    new_desc_text = read_text_arg(a.description, a.description_file)
    if a.name is None and new_desc_text is None and not a.category and not a.device:
        raise WBError("没有要修改的内容 / nothing to change (--name / --description[-file] / --category / --device)")
    old_cat = ids(detail.get("categories") or owned.get("categories"), "categoryId")
    old_dev = ids(detail.get("supportedDevices") or owned.get("supportedDevices"), "deviceId")
    cat_ids = _resolve_catalog_ids(a.category, cats, "分类") or old_cat
    dev_ids = _resolve_catalog_ids(a.device, devs, "设备") or old_dev
    name = a.name or detail.get("firmwareName") or owned.get("firmwareName")
    old_desc = detail.get("firmwareDescription") or owned.get("firmwareDescription") or ""
    fdesc = wrap_description(new_desc_text) if new_desc_text is not None else (old_desc if str(old_desc).lstrip().startswith("{") else wrap_description(str(old_desc)))
    fields = [("firmwareName", name), ("firmwareDescription", fdesc),
              ("categoryIds", json.dumps(cat_ids)), ("deviceIds", json.dumps(dev_ids))]
    plan = [("action", f"PATCH /firmwares/{a.firmware_id}"),
            ("note", "公开项目的修改会生成待审核修订，审核通过前页面仍显示旧内容"),
            ("firmwareName", f"{detail.get('firmwareName')} -> {name}"),
            ("categories", ", ".join(f"{i}:{cats.get(i)}" for i in cat_ids)),
            ("devices", ", ".join(f"{i}:{devs.get(i)}" for i in dev_ids)),
            ("description", "(不修改 keep)" if new_desc_text is None else unwrap_description(fdesc))]
    if owned.get("projectRevisionStatus") == "PENDING":
        plan.insert(1, ("warning", f"已有待审核修订 projectRevisionId={owned.get('projectRevisionId')}"))
    if not confirm(plan, "修改项目信息 / edit project", a):
        return 0
    body = request("PATCH", f"/firmwares/{a.firmware_id}", token=tok, fields=fields, timeout=120)
    _report(body)
    return 0


def _version_action(a: argparse.Namespace, title: str, method: str, suffix: str, note: str,
                    params: Optional[Dict[str, Any]] = None) -> int:
    tok = resolve_bearer(a.token)
    owned = owned_project(tok, a.firmware_id)
    vers = get_versions(a.firmware_id, tok)
    v = find_version(vers, a.version, a.version_id)
    vid = str(v.get("versionId"))
    plan = [("action", f"{method} /firmwares/{a.firmware_id}/versions/{vid}{suffix}" + (f" {params}" if params else "")),
            ("firmwareName", owned.get("firmwareName")), ("version", f"{v.get('versionName')} ({vid})"),
            ("current state", f"{v.get('status')} / {v.get('visibility')}" + ("  (current)" if v.get("current") else "")),
            ("note", note)]
    if method == "DELETE" and len(vers) == 1:
        plan.append(("warning", "这是项目唯一的版本，删除它会删除整个项目" +
                     ("（已加 --confirm-project-deletion）" if params and params.get("confirmFirmwareDeletion") == "true"
                      else "；服务器会拒绝，除非加 --confirm-project-deletion")))
    if not confirm(plan, title, a):
        return 0
    body = request(method, f"/firmwares/{a.firmware_id}/versions/{vid}{suffix}", token=tok, params=params)
    _report(body)
    return 0


def cmd_withdraw(a: argparse.Namespace) -> int:
    return _version_action(a, "下架版本 / withdraw", "POST", "/withdraw", "下架后其他用户不可见，可用 relist 申请重新上架")


def cmd_relist(a: argparse.Namespace) -> int:
    return _version_action(a, "申请重新上架 / relist request", "POST", "/relist-request", "提交后需官方审核")


def cmd_delete_version(a: argparse.Namespace) -> int:
    return _version_action(a, "删除版本 / delete version (不可恢复)", "DELETE", "", "删除不可恢复 / irreversible",
                           params={"confirmFirmwareDeletion": "true" if a.confirm_project_deletion else "false"})


# --------------------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="webburner.py", description=f"M5Stack Web Burner helper v{VERSION} (burner.m5stack.com)")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="command", required=True)

    def p(name: str, help_: str, auth: bool = True, js: bool = True) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_, description=help_)
        if auth:
            sp.add_argument("--token", help=argparse.SUPPRESS)
        if js:
            sp.add_argument("--json", action="store_true", help="原始 JSON 输出 / raw JSON")
        return sp

    def mut(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--dry-run", action="store_true", help="只显示将要发送的内容，不发请求 / show plan only")
        sp.add_argument("--yes", action="store_true", help="跳过交互确认 / skip the yes prompt")

    def ver(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("firmware_id")
        g = sp.add_mutually_exclusive_group(required=True)
        g.add_argument("--version", dest="version", help="版本名, e.g. v1.0")
        g.add_argument("--version-id")

    sp = p("login", "登录：从 Edge/Chrome 读取 m5burner.accessToken，或 --paste/--email", auth=False, js=False)
    sp.add_argument("--browser", choices=BROWSERS + ("all",), default="all")
    sp.add_argument("--profile", help='浏览器 profile 目录名, e.g. "Default" / "Profile 1"')
    sp.add_argument("--paste", action="store_true", help="手动粘贴 token（不回显）")
    sp.add_argument("--token", help="直接给 token（会留在 shell 历史里，优先用 --paste）")
    sp.add_argument("--email", help="用邮箱+密码登录（密码交互输入或 WEB_BURNER_PASSWORD）")
    sp.set_defaults(func=cmd_login)
    p("logout", "删除本地保存的 token", auth=False, js=False).set_defaults(func=cmd_logout)
    p("me", "当前登录账号 / whoami").set_defaults(func=cmd_me)
    sub._name_parser_map["whoami"] = sub._name_parser_map["me"]  # alias

    p("summary", "开发者总计：固件数/下载/点赞/关注/积分").set_defaults(func=cmd_summary)
    sp = p("stats", "统计：总计 + 每个项目的下载/点赞/状态/审核 + 各版本下载")
    sp.add_argument("--firmware-id")
    sp.add_argument("--no-versions", action="store_true")
    sp.set_defaults(func=cmd_stats)
    p("mine", "我的固件项目列表 (含私有/审核中)").set_defaults(func=cmd_mine)
    sp = p("my-versions", "我的所有版本")
    sp.add_argument("--firmware-id")
    sp.set_defaults(func=cmd_my_versions)
    sp = p("resolve-mine", "按名称在我的项目里找 firmwareId", js=False)
    sp.add_argument("name")
    sp.set_defaults(func=cmd_resolve_mine)

    sp = p("search", "公开搜索", auth=False)
    sp.add_argument("keyword", nargs="?", default="")
    sp.add_argument("--sort", default="LATEST")
    sp.add_argument("--page", type=int, default=1)
    sp.add_argument("--size", type=int, default=20)
    sp.set_defaults(func=cmd_search)
    sp = p("show", "项目详情（已登录时含自己的私有项目）")
    sp.add_argument("firmware_id")
    sp.set_defaults(func=cmd_show)
    sp = p("versions", "版本列表（已登录时 scope=ALL）")
    sp.add_argument("firmware_id")
    sp.set_defaults(func=cmd_versions)
    sp = p("edit-detail", "自己某个版本的可编辑详情（sourceUrl/描述/审核状态）")
    ver(sp)
    sp.set_defaults(func=cmd_edit_detail)
    p("categories", "分类列表 (id/name)", auth=False).set_defaults(func=cmd_catalog)
    p("devices", "设备列表 (id/name)", auth=False).set_defaults(func=cmd_catalog)
    sp = p("download", "下载某个版本的 bin 并显示 sha256", js=False)
    ver(sp)
    sp.add_argument("--output-dir", default=".")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_download)

    sp = p("create", "上传一个全新的固件项目 (POST /firmwares)", js=False)
    sp.add_argument("--name", required=True, help="项目名（服务器按名称区分项目）")
    sp.add_argument("--version", required=True, help="首个版本名, e.g. v1.0")
    sp.add_argument("--file", required=True, help="固件 .bin")
    sp.add_argument("--cover", required=True, help="封面图 (建议 1280x720 PNG/JPG/GIF)")
    sp.add_argument("--category", action="append", help="分类 id 或名称，可重复或逗号分隔")
    sp.add_argument("--device", action="append", help="设备 id 或名称，可重复或逗号分隔")
    sp.add_argument("--description"); sp.add_argument("--description-file", help="项目描述 Markdown 文件")
    sp.add_argument("--version-description"); sp.add_argument("--version-description-file")
    sp.add_argument("--source-url", help="源码/GitHub 链接")
    sp.add_argument("--visibility", default="PUBLIC", help="PUBLIC(需审核) / PRIVATE")
    mut(sp); sp.set_defaults(func=cmd_create)

    sp = p("add-version", "给已有项目上传新版本 (POST /firmwares，按项目名匹配)", js=False)
    sp.add_argument("firmware_id")
    sp.add_argument("--version", required=True, help="新版本名")
    sp.add_argument("--file", required=True)
    sp.add_argument("--cover", help="新封面；不填沿用旧封面")
    sp.add_argument("--version-description"); sp.add_argument("--version-description-file")
    sp.add_argument("--source-url", help="不填沿用项目当前 sourceUrl")
    sp.add_argument("--category", action="append", help="不填沿用当前分类")
    sp.add_argument("--device", action="append", help="不填沿用当前设备")
    sp.add_argument("--visibility", help="不填沿用项目当前可见性")
    mut(sp); sp.set_defaults(func=cmd_add_version)

    sp = p("edit-version", "修改某个版本 (PUT)：版本名/描述/sourceUrl/可见性/封面", js=False)
    ver(sp)
    sp.add_argument("--new-version-name")
    sp.add_argument("--version-description"); sp.add_argument("--version-description-file")
    sp.add_argument("--source-url"); sp.add_argument("--visibility"); sp.add_argument("--cover")
    mut(sp); sp.set_defaults(func=cmd_edit_version)

    sp = p("edit-project", "修改项目名/描述/分类/设备 (PATCH，公开项目需审核)", js=False)
    sp.add_argument("firmware_id")
    sp.add_argument("--name"); sp.add_argument("--description"); sp.add_argument("--description-file")
    sp.add_argument("--category", action="append"); sp.add_argument("--device", action="append")
    mut(sp); sp.set_defaults(func=cmd_edit_project)

    for name, fn, h in (("withdraw", cmd_withdraw, "下架某个版本"), ("relist", cmd_relist, "申请重新上架（需审核）")):
        sp = p(name, h, js=False); ver(sp); mut(sp); sp.set_defaults(func=fn)
    sp = p("delete-version", "删除某个版本（不可恢复）", js=False)
    ver(sp)
    sp.add_argument("--confirm-project-deletion", action="store_true", help="删除最后一个版本时允许连同项目一起删除")
    mut(sp); sp.set_defaults(func=cmd_delete_version)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except WBError as e:
        print(f"错误 / error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消 / cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
