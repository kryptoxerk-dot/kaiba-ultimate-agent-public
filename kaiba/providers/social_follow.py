"""Budgeted read-only follow graph queries; one shared $20/$0.50 ledger."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


class Unavailable(Exception):
    pass


def budget_reader():
    paths = [ROOT / "skills/twitterapi-research/scripts/research.py",
             ROOT / "data/codex-j7tracker-20261006/skills/twitterapi-research/scripts/research.py"]
    path = next((p for p in paths if p.is_file()), None)
    if path is None:
        raise Unavailable("shared_budget_reader_missing")
    spec = importlib.util.spec_from_file_location("kaiba_social_budget_reader", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def followings(handle, *, page_size=20, client=None, budget=None):
    """Only the newest page. Reserve the worst tier before dispatch; no refunds/retries."""
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle) or not 20 <= page_size <= 200:
        raise Unavailable("invalid_follow_query")
    reader = budget or budget_reader()
    try:
        reader.reserve(reader.DEFAULT_ROOT / "social-research-budget.json", page_size * 3)
        key = reader.credential()
    except Exception as exc:
        reason = str(exc)
        if reason not in {"daily_budget_exhausted","lifetime_budget_exhausted","invalid_budget_ledger",
                          "missing_credential","invalid_credential_file","budget_busy"}:
            reason = "budget_or_credential_unavailable"
        raise Unavailable(reason) from None
    own = client is None
    client = client or httpx.Client(timeout=12, follow_redirects=False)
    try:
        response = client.get("https://api.twitterapi.io/twitter/user/followings",
                              params={"userName":handle, "pageSize":page_size},
                              headers={"X-API-Key":key})
        if response.status_code != 200:
            raise Unavailable(f"following_http_{response.status_code}")
        if len(response.content) > 2_000_000:
            raise Unavailable("following_response_too_large")
        body = response.json()
        if not isinstance(body, dict) or body.get("status") != "success" or not isinstance(body.get("followings"), list):
            raise Unavailable("following_semantic_unavailable")
        if len(body["followings"]) > page_size:
            raise Unavailable("following_page_exceeds_budget")
        return body
    except (httpx.HTTPError, ValueError):
        raise Unavailable("following_network_or_shape_unavailable") from None
    finally:
        if own:
            client.close()


def normalize_user(raw):
    from urllib.parse import urlsplit

    if not isinstance(raw, dict):
        return None
    uid = str(raw.get("id") or "")
    handle = str(raw.get("userName") or raw.get("handle") or "").lstrip("@").lower()
    if not uid.isdigit() or not re.fullmatch(r"[a-z0-9_]{1,15}", handle):
        return None
    profile = raw.get("profile") or {}
    profile = profile if isinstance(profile, dict) else {}
    website = raw.get("website") or profile.get("url")
    if isinstance(website, dict):
        website = website.get("url")
    # TwitterAPI profile entities contain the expanded project URL, while `url`
    # on the user object is often the X profile itself.
    bio = raw.get("profile_bio") or {}
    if isinstance(bio, dict):
        links = ((bio.get("entities") or {}).get("url") or {}).get("urls") or []
        if links and isinstance(links[0], dict):
            website = links[0].get("expanded_url") or website
    # MEASURED live following pages use legacy `url` (usually t.co), not the
    # richer profile_bio object shown in the docs. Keep that profile link.
    if not website and isinstance(raw.get("url"),str):
        value=raw["url"]
        if urlsplit(value).hostname not in {"x.com","www.x.com","twitter.com","www.twitter.com"}:
            website=value
    return {"id":uid, "handle":handle, "name":str(raw.get("name") or profile.get("name") or "")[:200],
            "website":str(website or "")[:2048],
            "bio":str(raw.get("description") or profile.get("description") or "")[:4000],
            "parody": bool(raw.get("parody") or profile.get("parody")),
            "unavailable":bool(raw.get("unavailable") or raw.get("private") or raw.get("protected"))}


def public_https(url):
    """Fetch public HTTPS HTML with pinned resolved IP, host TLS and bounded redirects.

    Profile links are untrusted. Never send credentials, access private destinations,
    execute JavaScript or accept a certificate for a different hostname.
    """
    import http.client
    import ipaddress
    import socket
    import ssl
    import time
    from urllib.parse import urljoin, urlsplit

    deadline = time.monotonic() + 15
    for _ in range(3):
        parts = urlsplit(url)
        host = parts.hostname
        if parts.scheme != "https" or not host or parts.username or parts.password or parts.port not in (None,443):
            raise Unavailable("unsafe_project_url")
        addresses = sorted({a[4][0] for a in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)},key=lambda a:ipaddress.ip_address(a).version)
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise Unavailable("nonpublic_project_url")
        timeout = min(5, deadline-time.monotonic())
        if timeout <= 0:
            raise Unavailable("project_fetch_deadline")
        sock = socket.create_connection((addresses[0],443),timeout=timeout)
        try:
            sock = ssl.create_default_context().wrap_socket(sock,server_hostname=host)
            target = parts.path or "/"
            if parts.query:
                target += "?"+parts.query
            if any(c in target+host for c in "\r\n"):
                raise Unavailable("unsafe_project_url")
            sock.sendall((f"GET {target} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: KaibaProjectVerify/1\r\n"
                          "Accept-Encoding: identity\r\nConnection: close\r\n\r\n").encode("ascii"))
            response = http.client.HTTPResponse(sock)
            response.begin()
            if response.status in (301,302,303,307,308):
                url = urljoin(url,response.getheader("Location") or "")
                continue
            if response.status != 200 or "text/" not in (response.getheader("Content-Type") or ""):
                raise Unavailable("project_html_unavailable")
            body = bytearray()
            while len(body) <= 500_000 and time.monotonic() < deadline:
                chunk = response.read(8192)
                if not chunk:
                    return url, body.decode("utf-8",errors="replace")
                body.extend(chunk)
            raise Unavailable("project_response_or_deadline_limit")
        finally:
            sock.close()
    raise Unavailable("project_redirect_limit")


def has_backlink(html, handle):
    from html.parser import HTMLParser
    from urllib.parse import urlsplit

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.urls = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.urls.extend(v for k,v in attrs if k == "href" and v)
    links = Links()
    links.feed(html)
    return any(urlsplit(u).scheme == "https" and urlsplit(u).hostname in ("x.com","twitter.com","www.x.com","www.twitter.com")
               and urlsplit(u).path.strip("/").lower() == handle.lower() for u in links.urls)


def sync_rules(handles,*,client=None,budget=None,cache=Path("data/social-project-rules.json")):
    """Maintain only three owned project rules; authoritative GET before any mutation."""
    desired = sorted(set(handles))
    if len(desired)>20 or any(not re.fullmatch(r"[a-z0-9_]{1,15}",h) for h in desired):
        raise Unavailable("project_roster_bound")
    import json
    import time
    if cache is not None and cache.exists():
        try:
            state = json.loads(cache.read_text(encoding="utf-8"))
            if state["handles"]==desired and 0<=time.time()-state["checked_at"]<21600:
                return {"status":"cached_verified","projects":len(desired),"mutated_rules":0,"second_socket":False}
        except (OSError,ValueError,KeyError,TypeError):
            pass
    queries = []
    value = ""
    for handle in desired:
        next_value = value+(" OR " if value else "")+"from:"+handle
        if len(next_value)>255:
            queries.append(value)
            value = "from:"+handle
        else:
            value = next_value
    if value:
        queries.append(value)
    if len(queries)>3:
        raise Unavailable("project_rule_bound")
    reader = budget or budget_reader()
    key = reader.credential()
    own = client is None
    client = client or httpx.Client(timeout=10,follow_redirects=False)
    def request(method,path,body=None):
        reader.reserve(reader.DEFAULT_ROOT/"social-research-budget.json",150)
        r = client.request(method,"https://api.twitterapi.io"+path,headers={"X-API-Key":key},json=body)
        if r.status_code!=200:
            raise Unavailable(f"rule_http_{r.status_code}_no_blind_retry")
        d = r.json()
        if not isinstance(d,dict) or d.get("status")=="error" or d.get("success") is False:
            raise Unavailable("rule_semantic_unavailable")
        return d
    try:
        rules = request("GET","/oapi/tweet_filter/get_rules").get("rules")
        if not isinstance(rules,list):
            raise Unavailable("rules_shape_unavailable")
        active_other = sum(r.get("is_effect") in (1,True) for r in rules if not str(r.get("tag","")).startswith("kaiba-official-projects-v1-"))
        if active_other+len(queries)>15:
            raise Unavailable("shared_rule_capacity")
        changes = 0
        for i in range(3):
            tag = f"kaiba-official-projects-v1-{i+1}"
            found = [r for r in rules if r.get("tag")==tag]
            if len(found)>1:
                raise Unavailable("owned_rule_duplicate_requires_reconcile")
            row = found[0] if found else None
            query = queries[i] if i<len(queries) else None
            if query is None:
                if row and row.get("is_effect") in (1,True):
                    request("POST","/oapi/tweet_filter/update_rule",{**{k:row[k] for k in ("rule_id","tag","value")},"interval_seconds":5,"is_effect":0})
                    changes+=1
                continue
            if row is None:
                result = request("POST","/oapi/tweet_filter/add_rule",{"tag":tag,"value":query,"interval_seconds":5})
                rid = result.get("rule_id")
                if not rid:
                    raise Unavailable("rule_create_outcome_unknown")
                request("POST","/oapi/tweet_filter/update_rule",{"rule_id":rid,"tag":tag,"value":query,"interval_seconds":5,"is_effect":1})
                changes+=1
            elif row.get("value")!=query or row.get("is_effect") not in (1,True) or row.get("interval_seconds")!=5:
                request("POST","/oapi/tweet_filter/update_rule",{"rule_id":row["rule_id"],"tag":tag,"value":query,"interval_seconds":5,"is_effect":1})
                changes+=1
        after = request("GET","/oapi/tweet_filter/get_rules").get("rules",[]) if changes else rules
        covered = {h for r in after if str(r.get("tag","")).startswith("kaiba-official-projects-v1-") and r.get("is_effect") in (1,True)
                   for h in re.findall(r"from:([a-z0-9_]+)",str(r.get("value","")))}
        if set(desired)!=covered:
            raise Unavailable("project_coverage_not_confirmed")
        if cache is not None:
            cache.parent.mkdir(parents=True,exist_ok=True)
            temp = cache.with_suffix(".new")
            temp.write_text(json.dumps({"handles":desired,"checked_at":time.time()}),encoding="utf-8")
            temp.replace(cache)
        return {"status":"verified","projects":len(desired),"mutated_rules":changes,"second_socket":False}
    except (httpx.HTTPError,ValueError):
        raise Unavailable("rule_outcome_unavailable_no_blind_retry") from None
    finally:
        if own:
            client.close()
