# Process Availability — Inventory via Excel or JSON (Ansible + Monaco)

This guide explains how to add Process Availability checks to Dynatrace using our Ansible + Monaco pipeline. It covers both inventory formats:

* Excel (.xlsx) with a simple header
* JSON (the original format)

You'll learn where files live, what each field means, how match rules are built, how overrides/metadata work, and exactly how to run the workflow end-to-end.

## 0) What you edit (SSOT)

Your SSOT environment file points to the inventory sources you maintain:

```yaml
envs/<org>/<env>.yaml

Example (already in the repo):
id: cra
env: prod
url: https://<your-dynatrace-base-url>
token: DT_TOKEN_CRA_PROD

projects:
  - name: process-availability
    path: ansible/modules/process-availability

naming:
  pattern: "{{ org_alias }}-{{ env_alias }}-{{ module }}-{{ key }}"
  org_alias: cra
  env_alias: prod
  prefix: "bsc"

resources:
  process_availability:
    sources:
      - path: "process-monitoring_0.xlsx"
        defaults:
          matcher_key: executable_eq      # default key for all rows without an explicit key
          min: 1
          os: ["LINUX"]
          host_suffix: ""                 # appended to short hostnames (optional)
          host_prefix: ""                 # prepended to short hostnames (optional)
          host_match: contains            # exact | contains | regex
          metadata:
            Event-Category: APP
            Event-Recipient: SL-EMAIL
```

You can list multiple sources under sources: and mix Excel and JSON. The playbook merges them into one canonical inventory, handles overrides, and de-dupes.

## 1) Inventory via Excel

### 1.1 Template (headers)

Create/maintain an Excel file with this exact header row (order doesn't matter):

```text
HostName | HostMatch | ProcessName | MatcherValue | MatcherKey | Min | OS | HostSuffix | HostPrefix
```

### 1.2 Row meanings

* **HostName** (required): Short host (ec01ld4551-00) or FQDN (ec01ld4551-00.isvcs.net).
* **HostMatch** (optional): exact, contains, or regex. If blank, falls back to defaults.host_match in SSOT.
* **ProcessName** (required): Label used in rendered names/IDs. *Tip: This is not the Dynatrace matcher; it's the friendly name that appears in IDs/reports.*
* **MatcherValue** (required): The string used by the matcher (e.g., smpolicysrv).
* **MatcherKey** (optional): One of the keys below. If blank, uses defaults.matcher_key from SSOT.
  * `executable_eq` (exact process name; best for Linux daemons like smpolicysrv)
  * `executable_contains` / `executable_prefix` / `executable_suffix`
  * `path_eq` / `path_contains` / `path_prefix` / `path_suffix`
  * `cmd_eq` / `cmd_contains` / `cmd_prefix` / `cmd_suffix`
* **Min** (optional): Minimum number of processes to be up. If blank, uses defaults.min from SSOT.
* **OS** (optional): Comma-separated: LINUX, WINDOWS. Row values are added into the file's default OS set.
* **HostSuffix** (optional): First non-empty row value wins for the file; otherwise SSOT host_suffix is used.
* **HostPrefix** (optional): Same as suffix, but prefixed.

### 1.3 Example rows (exact match on executable)

We want exact process name smpolicysrv on Linux:

```text
HostName         HostMatch  ProcessName                    MatcherValue  MatcherKey      Min  OS     HostSuffix  HostPrefix
ec01ld4551-00    exact      sams_iss_SiteMinder_Policysrv  smpolicysrv   executable_eq   1    LINUX
ec01lp4551-00    exact      sams_iss_SiteMinder_Policysrv  smpolicysrv   executable_eq   1    LINUX
```

If your SSOT defaults already set matcher_key: executable_eq, you can leave MatcherKey blank in Excel rows and just fill MatcherValue=smpolicysrv.

## 2) Inventory via JSON (alternative)

A single JSON file equivalent to the Excel above:

```json
{
  "version": 1,
  "nameSuffix": ".isvcs.net",
  "defaults": {
    "min": 1,
    "os": ["LINUX"],
    "metadata": {
      "items": [
        { "metadataKey": "Event-Category",  "metadataValue": "APP" },
        { "metadataKey": "Event-Recipient", "metadataValue": "SL-EMAIL" }
      ]
    }
  },
  "hosts": [
    {
      "names": ["ec01ld4551-00", "ec01lp4551-00"],
      "processes": [
        {
          "name": "sams_iss_SiteMinder_Policysrv",
          "match": { "executable_eq": "smpolicysrv" },
          "min": 1,
          "os": ["LINUX"]
        }
      ]
    }
  ]
}
```

You can mix several JSON files and/or Excel files. The playbook merges them, de-dupes, and applies overrides.

## 3) How match rules are built

Every non-empty matcher key becomes one Dynatrace rule:

| Inventory key | Dynatrace property | Condition produced |
| --- | --- | --- |
| executable_eq | executable | $eq(<value>) |
| executable_contains | executable | $contains(<value>) |
| executable_prefix | executable | $prefix(<value>) |
| executable_suffix | executable | $suffix(<value>) |
| path_* | executablePath | $...(<value>) |
| cmd_* | commandLine | $...(<value>) |

Use executable_eq: smpolicysrv for precise Linux processes. Add cmd_contains if you need a specific CLI flag to differentiate roles. Do not wrap values in $eq(...) yourself—the template does that.

## 4) Defaults, overrides, and metadata

### Order of precedence

1. SSOT defaults (resources.process_availability.sources[].defaults merged across sources)
2. Host-level settings (JSON only)
3. Process-level settings (highest)

### OS resolution

```text
process.os → else host.os → else merged defaults.os
If none present, we fall back to ["LINUX","WINDOWS"]
```

### Metadata

* Normalized to a list of {metadataKey, metadataValue}.
* Keys de-dupe; nearest wins (process > host > defaults).

## 5) Naming and rendering

For each (host × process) we render:

* displayName: `<org>-<env>-procavail-<processName>-<fqdn>`
* Monaco config ID (sanitized): `<[prefix-]org-env-procavail-<processName>-<fqdn>>`
* Payload JSON: `ansible/modules/process-availability/payloads/<id>.json`
* Project config: `ansible/modules/process-availability/config.yaml` (refers to payloads; scope filled later)

We also de-dupe config IDs:
* Same (schema,id) and identical payload JSON → drop duplicates.
* Same (schema,id) but different payload JSON → rename later ones to id-<sha6>.

## 6) HOST scope resolution (online)

After payloads/config are rendered (offline), the resolver:

1. Infers the FQDN (xHostName, or from name/rules/filename).
2. Calls Dynatrace Entities v2 to find the HOST-… entity.
3. Writes scope: HOST-… into config.yaml.
4. Strips any legacy xHostName left in payloads.
5. Emits a resolve_skipped.txt with configId<TAB>fqdn for unresolved items (non-fatal).
6. Deploy guards later fail if any scope is not HOST-….

## 7) End-to-end workflow

### Option A — CI (Azure Pipelines)

Pipeline file: `pipelines/monaco-deploy.yaml`

Parameters:
```text
mission_space: cra (default) or statscan
environment: dev / prod (default prod)
run_mode: plan (default), dry-run, or apply
```

Stages:

#### Build (offline)

1. Installs Ansible+deps.
2. Resolves SSOT & sanity-checks inventory (`ssot_inventory_check.py`).
3. Merges inventory from Excel/JSON into `.generated/inventory_combined.json`.
4. Renders payloads into the project + manifest.yaml.
5. Publishes `.generated/` as an artifact.

#### Deploy (online; skipped when run_mode=plan)

1. Downloads `.generated` artifact to the self-hosted runner.
2. Runs Resolve HOST scopes (sets scope: HOST-… in project config.yaml).
3. Guards: no xHostName in payloads, all scopes are HOST-....
4. Monaco deploy (dry-run or apply depending on run_mode).
5. Safe prune (by name-prefix), removing repo-owned