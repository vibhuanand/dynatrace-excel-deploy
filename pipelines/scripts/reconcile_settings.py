#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse, json, sys, time
from pathlib import Path

try:
    import yaml
except Exception:
    print("PyYAML required. pip install pyyaml", file=sys.stderr); sys.exit(2)

import urllib.request, urllib.parse, urllib.error

# ---------------- HTTP ----------------
def http(method, base, path, token, payload=None, timeout=30):
    url = f"{base.rstrip('/')}{path}"
    headers = {"Authorization": f"Api-Token {token}", "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "ignore")
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore") if e.fp else ""
        raise RuntimeError(f"{method} {url} -> {e.code}\n{body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"{method} {url} -> {e}")

def list_settings(base, token, schema_id, page_size=200):
    """
    Paginate /api/v2/settings/objects and ensure we have:
      - objectId (for delete)
      - scope     (for matching)
      - value     (to get .name)
    NOTE: nextPageKey cannot be combined with other query params.
    """
    out, next_key = [], None
    while True:
        if next_key:
            qs = "?" + urllib.parse.urlencode({"nextPageKey": next_key})
        else:
            qs = "?" + urllib.parse.urlencode({
                "schemaIds": schema_id,
                "pageSize": str(page_size),
                "fields": "+objectId,+scope,+value",
            })
        _, body = http("GET", base, f"/api/v2/settings/objects{qs}", token)
        items = (body or {}).get("items") or []
        out.extend(items)
        next_key = (body or {}).get("nextPageKey")
        if not next_key:
            break
    return out

def delete_setting(base, token, object_id):
    code, _ = http("DELETE", base, f"/api/v2/settings/objects/{object_id}", token)
    return code in (200, 204)

# --------------- Desired set extraction ---------------
def load_desired(project_dir):
    """
    Desired = {(schema, scope, name)} derived from rendered project:
      - schema & scope from config.yaml (resolver wrote HOST-… scopes)
      - name from payload JSON ("name" field)
    """
    proj = Path(project_dir)
    cfgp = proj / "config.yaml"
    if not cfgp.is_file():
        raise RuntimeError(f"config.yaml not found: {cfgp}")
    cfg = yaml.safe_load(cfgp.read_text(encoding="utf-8")) or {}
    desired = set()
    for it in (cfg.get("configs") or []):
        t = ((it.get("type") or {}).get("settings") or {})
        schema = (t.get("schema") or "").strip()
        scope  = (t.get("scope")  or "").strip()
        templ  = ((((it.get("config") or {}).get("template")) or "").strip())
        if not (schema and scope and templ):
            continue
        p = proj / templ
        if not p.is_file():
            continue
        try:
            val = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        nm = (val.get("name") or "").strip()
        if not nm:
            continue
        desired.add((schema, scope, nm))
    return desired

# --------------- Main ---------------
def main():
    ap = argparse.ArgumentParser(description="Safe prune by name-prefix for Dynatrace Settings 2.0")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--project-dir", required=True, help="Rendered Monaco project dir")
    ap.add_argument("--schema", required=True, help="e.g., builtin:processavailability")
    ap.add_argument("--name-prefix", required=True, help="Repo-owned prefix (e.g., bsc-cra-prod-procavail-)")
    ap.add_argument("--mode", choices=["dry-run","apply"], default="dry-run")
    args = ap.parse_args()

    desired = load_desired(args.project_dir)  # {(schema, scope, name)}
    actual  = list_settings(args.base_url, args.token, args.schema)

    # Filter only objects that belong to this repo by name prefix
    ours = []
    for obj in actual:
        if not isinstance(obj, dict):
            continue
        oid = obj.get("objectId")
        scope = obj.get("scope") or ""
        val = obj.get("value") or {}
        nm = (val.get("name") or "")
        if not nm.startswith(args.name_prefix):
            continue
        if not oid:
            # Defensive: skip any item missing objectId
            # (shouldn't happen when fields are requested correctly)
            continue
        key = (args.schema, scope, nm)
        ours.append({"id": oid, "key": key, "name": nm})

    desired_keys = set(desired)
    our_keys     = {o["key"] for o in ours}
    orphans = [o for o in ours if o["key"] not in desired_keys]

    print(f"[reconcile] desired={len(desired_keys)} ours={len(our_keys)} orphans={len(orphans)} mode={args.mode}")
    for o in orphans:
        oid, key, nm = o["id"], o["key"], o["name"]
        print(f" - DELETE {oid}  key={key}  name={nm}")
        if args.mode == "apply":
            ok = delete_setting(args.base_url, args.token, oid)
            print(f"   -> {'deleted' if ok else 'failed'}")
            time.sleep(0.05)
    print("[reconcile] done.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
