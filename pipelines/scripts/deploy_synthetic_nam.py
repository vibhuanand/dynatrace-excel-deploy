#!/usr/bin/env python3
import os, sys, json, argparse, urllib.request, urllib.error

def api(base, path, token, method="GET", body=None):
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Api-Token {token}")
    req.add_header("Accept", "application/json; charset=utf-8")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, data=data, timeout=60) as r:
            raw = r.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="ignore")
        raise SystemExit(f"HTTP {e.code} {url}\n{msg}")
    except urllib.error.URLError as e:
        raise SystemExit(f"ERROR calling {url}: {e}")

def find_by_name(base, token, name):
    # List monitors and match by name (no name filter server-side)
    res = api(base, "/api/v2/synthetic/monitors", token, "GET")
    for it in (res or []):
        if it.get("name") == name:
            return it
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--config", required=True)       # .generated/.../config.yaml
    ap.add_argument("--payload-dir", required=True)  # .generated/.../payloads
    ap.add_argument("--mode", choices=["apply","dry-run"], default="dry-run")
    args = ap.parse_args()

    try:
        import yaml
    except Exception:
        raise SystemExit("Please install pyyaml")

    cfg = yaml.safe_load(open(args.config,"r",encoding="utf-8")) or {}
    items = cfg.get("configs") or []

    changed = 0
    planned = 0

    for c in items:
        name = c.get("name")
        if not name: 
            continue
        fname = "".join([ (ch if ch.isalnum() or ch in "._:-" else "_") for ch in name ])
        ppath = os.path.join(args.payload_dir, f"{fname}.json")
        if not os.path.isfile(ppath):
            print(f"SKIP: payload missing for '{name}'"); continue

        body = json.load(open(ppath,"r",encoding="utf-8"))
        exists = find_by_name(args.base_url, args.token, name)

        if exists:
            eid = exists.get("entityId")
            print(f"UPDATE: {name} ({eid})")
            planned += 1
            if args.mode == "apply":
                api(args.base_url, f"/api/v2/synthetic/monitors/{eid}", args.token, "PUT", body)
                changed += 1
        else:
            print(f"CREATE: {name}")
            planned += 1
            if args.mode == "apply":
                api(args.base_url, "/api/v2/synthetic/monitors", args.token, "POST", body)
                changed += 1

    print(f"Done. planned={planned} changed={changed} mode={args.mode}")

if __name__ == "__main__":
    main()
