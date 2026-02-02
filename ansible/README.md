This directory contains the Ansible playbook used to orchestrate Monaco deployments.

* `run-monaco.yml` – Reads an environment YAML file, extracts the Dynatrace token name and environment name, prepares the token in the runtime environment, and calls `monaco deploy` with or without `--dry-run`. You can limit deployment to specific projects by listing them in the env YAML under the `projects` key.

To execute the playbook locally:

```
export DT_TOKEN_TC_DEV=<your_dynatrace_token>
ansible-playbook ansible/run-monaco.yml -e env_file=envs/transport-canada/dev.yaml -e dry_run_only=true
```

When used via GitHub Actions, the token is provided from a secret and exported by the workflow. See the root `README.md` for full instructions.