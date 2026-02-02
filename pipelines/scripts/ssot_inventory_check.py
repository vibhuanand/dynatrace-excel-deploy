# pipelines/scripts/ssot_inventory_check.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, json, os, sys
from typing import Any, Dict, List

try:
    import yaml
except Exception:
    print("##vso[task.logissue type=error]PyYAML is required. pip install pyyaml", file=sys.stderr)
    sys.exit(2)


def normalize_sources(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return a normalized list of {'path': str, 'defaults': dict} for both modern and legacy formats."""
    resources = cfg.get("resources") or {}

    # Modern
    mod = resources.get("process_availability")
    if isinstance(mod, dict):
        srcs = mod.get("sources")
        out: List[Dict[str, Any]] = []
        if isinstance(srcs, list):
            for s in srcs:
                if isinstance(s, str):
                    out.append({"path": s, "defaults": {}})
                elif isinstance(s, dict) and "path" in s:
                    d = dict(s)
                    d.setdefault("defaults", {})
                    out.append({"path": d["path"], "defaults": d["defaults"]})
        return out

    # Legacy
    legacy = resources.get("process_monitoring_inventory")
    out: List[Dict[str, Any]] = []
    if legacy:
        if isinstance(legacy, list):
            for it in legacy:
                out.append({"path": it, "defaults": {}})
        else:
            out.append({"path": legacy, "defaults": {}})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve SSOT absolute path and sanity-check inventory source files.")
    ap.add_argument("--env-file", required=True, help="Path to SSOT YAML (e.g., envs/cra/prod.yaml)")
    args = ap.parse_args()

    env_file = args.env_file
    if not os.path.isfile(env_file):
        print(f"##vso[task.logissue type=error]Missing env file: {env_file}")
        return 2

    ssot_abs = os.path.abspath(env_file)
    print(f"Resolved ENV_FILE={env_file}")
    print(f"SSOT_ABS={ssot_abs}")
    # Export for later tasks
    print(f"##vso[task.setvariable variable=SSOT_ABS]{ssot_abs}")

    # Load YAML
    try:
        with open(ssot_abs, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"##vso[task.logissue type=error]Failed to read/parse YAML: {e}")
        return 2

    # Normalize sources
    sources = normalize_sources(cfg)
    print(f"Sources: {json.dumps(sources, ensure_ascii=False)}")

    # Validate files
    ssot_dir = os.path.dirname(ssot_abs)
    ok = True
    for src in sources:
        p = (src or {}).get("path", "")
        if not p:
            print("ERROR: Source entry missing required 'path'")
            ok = False
            continue
        abs_p = p if os.path.isabs(p) else os.path.abspath(os.path.join(ssot_dir, p))
        if not os.path.isfile(abs_p):
            print(f"ERROR: inventory file missing -> {abs_p}")
            ok = False
        else:
            print(f"OK: {abs_p}")

    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
