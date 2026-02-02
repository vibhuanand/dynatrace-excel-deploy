#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
resolve_host_scopes.py
 - Reads the rendered Monaco project (config.yaml + payloads/*.json)
 - Determines the target HOST for each payload (from xHostName or from name/rules)
 - Resolves HOST entityId via Dynatrace Entities v2
 - Writes the resolved 'scope' back into config.yaml
 - Strips legacy xHostName from payload JSONs
 - Enforces unique Monaco config IDs:
     * exact duplicates (same id + identical payload JSON) -> drop later entries
     * near-duplicates (same id but different payload)     -> rename to 'id-<sha6>'
 - Emits resolve_skipped.txt with "<configId>\t<fqdnOrShort>" for unresolved items

Inputs (either flags or env vars):
  --base-url / DT_BASE_URL
  --token    / DT_TOKEN or DT_TOKEN_CRA_PROD
  --payload-dir / PAYLOAD_DIR (defaults to $GEN_DIR/ansible/modules/process-availability/payloads)
  --config-path / CONFIG_PATH (defaults to $GEN_DIR/ansible/modules/process-availability/config.yaml)

Exit code is always 0 so the pipeline can continue; later guard steps enforce correctness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple, List, Dict

try:
    import yaml  # PyYAML
except Exception:
    print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


# ---------------- HTTP helpers ----------------

def http_get_json(base_url: str, token: str, path_qs: str, timeout: int = 30) -> dict:
    url = f"{base_url.rstrip('/')}{path_qs}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Api-Token {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} GET {url}\n{body.decode('utf-8','ignore')}")
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore") if e.fp else ""
        raise RuntimeError(f"HTTP {e.code} GET {url}\n{body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"URL error GET {url}: {e}") from e


# ---------------- FQDN / host extraction helpers ----------------

_FQDN_TAIL_RE = re.compile(r'([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\s*$')

def fqdn_from_tail(name: str) -> Optional[str]:
    if not isinstance(name, str):
        return None
    m = _FQDN_TAIL_RE.search(name.strip())
    return m.group(1) if m else None

def fqdn_from_procavail_string(s: str) -> Optional[str]:
    """
    Works for filename stems or displayName like:
      'cra-prod-procavail-<proc>-ec01ld4551-00-isvcs-net'
    Strategy:
      - Find last '-procavail-'
      - Tokenize tail by '-'
      - Drop first token (<proc>), last two tokens are domain parts
    """
    if not isinstance(s, str) or not s:
        return None
    marker = "-procavail-"
    ix = s.rfind(marker)
    tail = s[ix + len(marker):] if ix >= 0 else s
    toks = [t for t in tail.split("-") if t]
    if len(toks) < 3:
        return None
    host_tokens = toks[1:-2] if len(toks) >= 4 else toks[:-2]
    domain_tokens = toks[-2:]
    host = "-".join(host_tokens).strip("-")
    domain = ".".join(domain_tokens)
    if host and re.fullmatch(r"[A-Za-z0-9-]+", host) and re.fullmatch(r"[A-Za-z0-9-]+\.[A-Za-z]{2,}", domain):
        return f"{host}.{domain}"
    return None

def short_host_from_procavail_string(s: str) -> Optional[str]:
    """
    Fallback for environments that only use short hostnames.
    Tail after '-procavail-' is '<proc>-<host-short...>'. Return everything AFTER the first token.
    Example:
      'cra-prod-procavail-MyProc-ec01lp4424a'      -> 'ec01lp4424a'
      'cra-prod-procavail-MyProc-ec01-foo-123'     -> 'ec01-foo-123'
    """
    if not isinstance(s, str) or not s:
        return None
    marker = "-procavail-"
    ix = s.lower().rfind(marker)
    tail = s[ix + len(marker):] if ix >= 0 else s
    parts = [t for t in tail.split("-") if t]
    if len(parts) >= 2:
        return "-".join(parts[1:])
    return None

def extract_token_from_rules(rules) -> Optional[str]:
    """
    Try to recover a process token from payload['rules'].
    Prefer property 'executable', and $eq(...) over contains/prefix/suffix.
    """
    if not isinstance(rules, list):
        return None

    def parse_cond(cond: str) -> Optional[Tuple[int, str]]:
        m = re.search(r'\$(eq|contains|prefix|suffix)\(([^)]+)\)', cond or "")
        if not m:
            return None
        op = m.group(1)
        val = m.group(2)
        op_pri = {"eq": 0, "contains": 1, "prefix": 2, "suffix": 3}.get(op, 9)
        return op_pri, val

    best: Optional[Tuple[int, int, str]] = None  # (prop_pri, op_pri, val)
    for r in rules:
        if not isinstance(r, dict):
            continue
        prop = (r.get("property") or "").strip()
        cond = r.get("condition") or ""
        parsed = parse_cond(cond)
        if not parsed:
            continue
        op_pri, val = parsed
        prop_pri = 9
        if prop == "executable":
            prop_pri = 0
        elif prop == "commandLine":
            prop_pri = 1
        elif prop in ("executablePath", "executablepath", "executable_path"):
            prop_pri = 2
        cand = (prop_pri, op_pri, val)
        if best is None or cand < best:
            best = cand
    return best[2] if best else None

def fqdn_from_name_and_rules(name: str, rules) -> Optional[str]:
    token = extract_token_from_rules(rules)
    if not (isinstance(name, str) and token):
        return None
    needle = f"-{token}-"
    i = name.lower().rfind(needle.lower())
    if i == -1:
        return None
    cand = name[i + len(needle):].strip()
    return cand if _FQDN_TAIL_RE.match(cand) else None


# ---------------- Dynatrace HOST lookup (paginated & stricter) ----------------

def try_lookup_host(base_url: str, token: str, fqdn_or_short: str) -> Optional[str]:
    """
    Strategy (with pagination and stricter filtering):
      1) entityName("<fqdn_or_short>")  [paginated]
      2) entityName("<short>")          [paginated]
      3) entityName.contains("<short>") [paginated] + strict match of hostNames/hostName when FQDN is known
         (and prefer exact short-name matches when only a short was given)
    Returns HOST-xxxx or None.
    """
    if not fqdn_or_short:
        return None

    def _paged_entities(selector: str, fields: str = "", page_size: int = 500) -> list:
        results, next_key = [], None
        while True:
            params = {"entitySelector": selector, "pageSize": str(page_size)}
            if fields:
                params["fields"] = fields
            if next_key:
                # nextPageKey cannot be combined with other params
                qs = "?" + urllib.parse.urlencode({"nextPageKey": next_key})
            else:
                qs = "?" + urllib.parse.urlencode(params)
            data = http_get_json(base_url, token, f"/api/v2/entities{qs}")
            items = (data or {}).get("entities") or []
            results.extend([e for e in items if isinstance(e.get("entityId"), str) and e["entityId"].startswith("HOST-")])
            next_key = (data or {}).get("nextPageKey")
            if not next_key:
                break
        return results

    # accept short input; derive short component for contains() searches
    short = fqdn_or_short.split(".", 1)[0]

    # 1) exact match on provided name (FQDN or short)
    for cand in (fqdn_or_short, short):
        sel = f'type(HOST),entityName("{cand}")'
        ents = _paged_entities(sel)
        # Prefer exact displayName match, else first HOST-
        for e in ents:
            dn = (e.get("displayName") or "").strip()
            if dn.lower() == cand.lower():
                return e["entityId"]
        if ents:
            return ents[0]["entityId"]

    # 2) contains(short) + strict filter if we had a real FQDN
    want_fqdn = fqdn_or_short.lower() if "." in fqdn_or_short else None
    ents = _paged_entities(f'type(HOST),entityName.contains("{short}")', fields="+properties")
    if not ents:
        return None

    if want_fqdn:
        strict = []
        for e in ents:
            props = e.get("properties") or {}
            hostnames = [h.lower() for h in (props.get("hostNames") or []) if isinstance(h, str)]
            primary = (props.get("hostName") or "").lower()
            if (want_fqdn in hostnames) or (primary == want_fqdn):
                strict.append(e)
        if len(strict) == 1:
            return strict[0]["entityId"]
        if len(strict) > 1:
            # choose the one whose displayName equals FQDN, else first
            for e in strict:
                if (e.get("displayName") or "").strip().lower() == want_fqdn:
                    return e["entityId"]
            return strict[0]["entityId"]

    # NEW: if we're matching a short name (no dot), prefer exact short matches
    if not want_fqdn:
        strict_short = []
        for e in ents:
            dn = (e.get("displayName") or "").strip().lower()
            primary = ((e.get("properties") or {}).get("hostName") or "").strip().lower()
            if dn == short.lower() or primary == short.lower():
                strict_short.append(e)
        if len(strict_short) == 1:
            return strict_short[0]["entityId"]
        if len(strict_short) > 1:
            for e in strict_short:
                if (e.get("displayName") or "").strip().lower() == short.lower():
                    return e["entityId"]
            return strict_short[0]["entityId"]

    # fallback: first candidate (works when short names are unique)
    return ents[0]["entityId"] if ents else None


# ---------------- payload helpers (RESTORED) ----------------

def load_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_json(path: str, obj: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def strip_xhostname_in_payloads(payload_dir: str) -> int:
    removed = 0
    for root, _, files in os.walk(payload_dir):
        for fn in files:
            if not fn.lower().endswith(".json"):
                continue
            p = os.path.join(root, fn)
            obj = load_json(p)
            if not isinstance(obj, dict):
                continue
            if "xHostName" in obj:
                obj.pop("xHostName", None)
                save_json(p, obj)
                removed += 1
    return removed


# ---------------- de-dupe helpers (Monaco uniqueness) ----------------

def _sha1_of_file(path: str) -> str:
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""

def enforce_unique_config_ids(cfg_path: str, payload_dir: str) -> None:
    """
    Enforces unique config IDs per settings schema for Monaco.

    Rules (matching requirement "skip only duplicates with same host, process, matcher"):
      - Use payload JSON file content-hash as a proxy for (host, process, matcher).
      - If two configs share same (schema, id) AND their payload hashes are equal -> drop later entries.
      - If two configs share same (schema, id) BUT payload hashes differ -> rename later entries to 'id-<sha6>'.
    """
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return

    items = cfg.get("configs") or []
    if not isinstance(items, list) or not items:
        return

    used_keys = set()                             # set[(schema, id)]
    seen_hashes: Dict[Tuple[str, str], set] = {}  # (schema,id)->{payload_hash}
    drop_indices = set()

    def payload_hash_for(item: dict) -> str:
        templ = ((((item.get("config") or {}).get("template")) or "").strip())
        p = os.path.join(payload_dir, os.path.basename(templ)) if templ else ""
        return _sha1_of_file(p) if p and os.path.isfile(p) else "nohash"

    for idx, it in enumerate(items):
        t = ((it.get("type") or {}).get("settings") or {})
        schema = (t.get("schema") or "").strip()
        cid = (it.get("id") or "").strip()
        if not schema or not cid:
            continue

        key = (schema, cid)
        ph = payload_hash_for(it)

        if key not in seen_hashes:
            seen_hashes[key] = {ph}
            used_keys.add(key)
            continue

        if ph in seen_hashes[key]:
            # exact duplicate -> drop
            drop_indices.add(idx)
        else:
            # different payload under same id -> rename with stable 6-char hash
            suffix = (ph or "dup")[:6]
            new_id = f"{cid}-{suffix}"
            new_key = (schema, new_id)
            n = 2
            while new_key in used_keys:
                new_id = f"{cid}-{suffix}-{n}"
                new_key = (schema, new_id)
                n += 1
            it["id"] = new_id
            used_keys.add(new_key)
            seen_hashes.setdefault(new_key, set()).add(ph)

    cfg["configs"] = [it for i, it in enumerate(items) if i not in drop_indices]
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)

    print(f"[dedupe] Dropped={len(drop_indices)}; Total after de-dupe={len(cfg['configs'])}")


# ---------------- path helpers ----------------

def resolve_paths_from_env_or_defaults(args_payload: Optional[str], args_config: Optional[str]) -> tuple[str, str]:
    if args_payload and args_config:
        return os.path.abspath(args_payload), os.path.abspath(args_config)

    payload_dir = os.environ.get("PAYLOAD_DIR")
    config_path = os.environ.get("CONFIG_PATH")
    if payload_dir and config_path:
        return os.path.abspath(payload_dir), os.path.abspath(config_path)

    ws = os.environ.get("GEN_DIR") or (os.path.join(os.environ.get("PIPELINE_WORKSPACE", ""), "generated"))
    if ws:
        proj = Path(ws) / "ansible" / "modules" / "process-availability"
        return str(proj / "payloads"), str(proj / "config.yaml")

    # Last resort (common on self-hosted Windows runner pathing)
    proj = Path(r"C:\agent\_work\generated") / "ansible" / "modules" / "process-availability"
    return str(proj / "payloads"), str(proj / "config.yaml")


# ---------------- main ----------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve Dynatrace HOST scopes and clean payloads")
    ap.add_argument("--base-url", required=False)
    ap.add_argument("--token", required=False)
    ap.add_argument("--payload-dir", required=False)
    ap.add_argument("--config-path", required=False)
    args = ap.parse_args()

    base_url = (args.base_url or os.getenv("DT_BASE_URL") or "").strip()
    token = (args.token or os.getenv("DT_TOKEN") or os.getenv("DT_TOKEN_CRA_PROD") or "").strip()
    payload_dir, cfg_path = resolve_paths_from_env_or_defaults(args.payload_dir, args.config_path)

    print(f"[resolver] GEN_DIR={os.environ.get('GEN_DIR','')}", file=sys.stderr)
    print(f"[resolver] PAYLOAD_DIR={payload_dir}", file=sys.stderr)
    print(f"[resolver] CONFIG_PATH={cfg_path}", file=sys.stderr)

    if not base_url or not token:
        print("ERROR: base-url/token not provided and DT_BASE_URL/DT_TOKEN env vars are empty.", file=sys.stderr)
        return 2
    if not os.path.isdir(payload_dir):
        print(f"ERROR: payload-dir not found: {payload_dir}", file=sys.stderr)
        return 2
    if not os.path.isfile(cfg_path):
        print(f"ERROR: config-path not found: {cfg_path}", file=sys.stderr)
        return 2

    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    items = cfg.get("configs")
    if not isinstance(items, list) or len(items) == 0:
        removed = strip_xhostname_in_payloads(payload_dir)
        print(f"No 'configs' to resolve (empty project). Stripped xHostName from {removed} payload(s).")
        # still emit empty skipped file
        try:
            out_dir = os.environ.get("GEN_DIR") or os.path.dirname(os.path.abspath(cfg_path))
            os.makedirs(out_dir, exist_ok=True)
            open(os.path.join(out_dir, "resolve_skipped.txt"), "w", encoding="utf-8").close()
        except Exception:
            pass
        return 0

    resolved = 0
    failures: List[str] = []
    skipped_unresolved: List[str] = []
    new_items: List[dict] = []

    for it in items:
        cid = (it.get("id") or "").strip()
        templ = ((((it.get("config") or {}).get("template")) or "").strip())
        if not cid or not templ:
            failures.append(f"[{cid or '?'}] id/template missing in config.yaml")
            continue

        pfile = os.path.basename(templ)
        pjson = os.path.join(payload_dir, pfile)
        pobj = load_json(pjson)
        if not isinstance(pobj, dict):
            failures.append(f"[{cid}] payload unreadable: {pjson}")
            continue

        name = pobj.get("name") or pobj.get("displayName") or ""
        rules = pobj.get("rules") or []

        # Preferred: explicit xHostName
        fqdn_or_short = (pobj.get("xHostName") or "").strip()

        # Fallbacks (in order)
        if not fqdn_or_short and isinstance(name, str):
            fqdn_or_short = fqdn_from_procavail_string(name)  # full FQDN from pattern
        if not fqdn_or_short:
            fqdn_or_short = fqdn_from_name_and_rules(name, rules)  # FQDN via token splice
        if not fqdn_or_short:
            fqdn_or_short = fqdn_from_procavail_string(Path(pjson).stem)  # FQDN from filename
        if not fqdn_or_short and isinstance(name, str):
            fqdn_or_short = fqdn_from_tail(name)  # trailing FQDN at end of name
        if not fqdn_or_short:
            # short host fallback (works with inventories that store only short names)
            cand = short_host_from_procavail_string(name) or short_host_from_procavail_string(Path(pjson).stem)
            if cand:
                fqdn_or_short = cand

        if not fqdn_or_short:
            failures.append(f"[{cid}] could not determine hostname from payload: {pjson}")
            continue

        try:
            eid = try_lookup_host(base_url, token, fqdn_or_short)
        except Exception as e:
            failures.append(f"[{cid}] lookup failed for host '{fqdn_or_short}': {e}")
            skipped_unresolved.append(f"{cid}\t{fqdn_or_short}")
            continue

        if not (isinstance(eid, str) and eid.startswith("HOST-")):
            failures.append(f"[{cid}] could not resolve HOST entity for '{fqdn_or_short}'")
            skipped_unresolved.append(f"{cid}\t{fqdn_or_short}")
            continue

        it.setdefault("type", {}).setdefault("settings", {})["scope"] = eid
        new_items.append(it)
        print(f"Resolved [{cid}] {fqdn_or_short} -> {eid}")
        resolved += 1

        # persist progressive progress (helps debug partial failures)
        cfg["configs"] = new_items
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)

    removed = strip_xhostname_in_payloads(payload_dir)
    print(f"Stripped xHostName from {removed} payload(s).")
    print(f"Resolved scopes: {resolved}")

    # Enforce unique IDs after resolution
    try:
        enforce_unique_config_ids(cfg_path, payload_dir)
    except Exception as e:
        print(f"Warning: enforce_unique_config_ids failed: {e}", file=sys.stderr)

    # Write skipped list for downstream step (non-fatal)
    try:
        out_dir = os.environ.get("GEN_DIR") or os.path.dirname(os.path.abspath(cfg_path))
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "resolve_skipped.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            for row in skipped_unresolved:
                f.write(row + "\n")
        if skipped_unresolved:
            print(f"Non-fatal: skipped {len(skipped_unresolved)} unresolved scope(s) -> {out_path}")
    except Exception as e:
        print(f"Warning: could not write resolve_skipped.txt: {e}", file=sys.stderr)

    # Sanity: count HOST scopes written
    try:
        data = yaml.safe_load(open(cfg_path, 'r', encoding='utf-8')) or {}
        scopes = [(((it.get('type') or {}).get('settings') or {}).get('scope') or '') for it in (data.get('configs') or [])]
        got = sum(1 for s in scopes if isinstance(s, str) and s.startswith('HOST-'))
        print(f"[resolver] config.yaml scopes set: {got}/{len(scopes)}")
    except Exception:
        pass

    # Always return 0; pipeline guard steps enforce correctness later
    return 0


if __name__ == "__main__":
    sys.exit(main())
