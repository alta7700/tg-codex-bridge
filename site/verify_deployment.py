#!/usr/bin/env python3
"""Verify HTTPS and Basic Auth without displaying or passing credentials as argv."""
import base64
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


class CheckFailure(Exception):
    def __init__(self, label, status=None):
        self.label = label
        self.status = status
        super().__init__(label)


def require(condition, label, status=None):
    if not condition:
        raise CheckFailure(label, status)


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin):
        super().__init__()
        self.allowed_origin = urllib.parse.urlsplit(origin)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p = urllib.parse.urlsplit(newurl)
        if p.scheme != "https" or p.netloc != self.allowed_origin.netloc:
            raise ValueError("Cross-origin redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def main():
    report_path = ROOT / "site/build/deployment-validation.json"
    report_path.write_text(json.dumps({"state":"RUNNING"}) + "\n")
    creds = json.loads((ROOT / ".runtime/sud-home-private/credentials.json").read_text())
    origin = creds["url"]
    assert urllib.parse.urlsplit(origin).scheme == "https"
    config = json.loads((ROOT / "site/content/ui.json").read_text())
    encoded = base64.b64encode((creds["username"] + ":" + creds["password"]).encode()).decode()
    opener = urllib.request.build_opener(SameOriginRedirect(origin))
    checks = []

    def request(path, auth=False, method="GET"):
        headers = {"User-Agent": "sud-home-deployment-check"}
        if auth:
            headers["Authorization"] = "Basic " + encoded
        try:
            req = urllib.request.Request(origin + path, headers=headers, method=method)
            with opener.open(req, timeout=25) as r:
                body = r.read() if method == "GET" else b""
                return r.status, body, dict(r.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, b"", {}

    probe_paths = ["/" + config["protected_image_probe"], "/" + config["protected_pdf_probe"]]
    for path in ["/", "/assets/content.json", "/search/index.json", *probe_paths]:
        code, _, _ = request(path, method="HEAD")
        require(code == 401, "anonymous_access_must_be_denied", code)
        checks.append({"path": path, "anonymous_status": code})
    code, body, headers = request("/", auth=True)
    require(code == 200, "authenticated_index_status", code)
    require(hashlib.sha256(body).hexdigest() == hashlib.sha256((ROOT / "site/dist/index.html").read_bytes()).hexdigest(),
            "deployed_index_hash")
    code, body, _ = request("/release.json", auth=True)
    require(code == 200, "authenticated_manifest_status", code)
    remote = json.loads(body)
    local = json.loads((ROOT / "site/dist/release.json").read_text())
    require(remote == local, "deployed_manifest_matches_local")
    for path in ["/assets/content.json", *probe_paths]:
        code, _, _ = request(path, auth=True, method="HEAD")
        require(code == 200, "authenticated_source_status", code)
        checks.append({"path": path, "authorized_status": code})
    report = {"state": "PASS", "url": origin, "release_id": local["release_id"],
              "verified_at": datetime.now(ZoneInfo("Europe/Moscow")).isoformat(),
              "TLS_certificate_verified": True, "basic_auth_all_sampled_paths": True,
              "remote_manifest_equals_local": True, "remote_index_sha256_equals_local": True,
              "checks": checks, "credential_output": False,
              "security_headers": {k:v for k,v in headers.items() if k.lower() in
                                   {"content-security-policy","x-robots-tag","referrer-policy","x-content-type-options","cache-control"}}}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"state":"PASS","url":origin,"release_id":local["release_id"],"checked_paths":len(checks)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        failure = {"state":"FAIL","reason_type":type(e).__name__,"credentials_printed":False}
        if isinstance(e, CheckFailure):
            failure.update(failed_check=e.label, http_status=e.status)
        (ROOT / "site/build/deployment-validation.json").write_text(json.dumps(failure,indent=2) + "\n")
        print(json.dumps(failure))
        raise SystemExit(1)
