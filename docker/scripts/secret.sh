#!/usr/bin/env bash
# secret — read/write credentials from the vault
#
# usage:
#   secret get fireflies.api_key        → prints the value
#   secret set fireflies.api_key "xyz"  → stores the value
#   secret list                          → lists all keys (dot paths)
#   secret delete fireflies.api_key     → removes the key
#
# vault location: /data/secrets/vault.yaml (chmod 600)
# format: simple 2-level YAML (service.key: value)

set -euo pipefail

VAULT_DIR="/data/secrets"
VAULT_FILE="$VAULT_DIR/vault.yaml"

# ensure vault exists
mkdir -p "$VAULT_DIR"
chmod 700 "$VAULT_DIR"
[ -f "$VAULT_FILE" ] || touch "$VAULT_FILE"
chmod 600 "$VAULT_FILE"

usage() {
    echo "usage: secret <get|set|list|delete> [path] [value]" >&2
    exit 1
}

[ $# -lt 1 ] && usage

CMD="$1"

case "$CMD" in
    get)
        [ $# -lt 2 ] && usage
        DOTPATH="$2"
        _VAULT_FILE="$VAULT_FILE" _DOTPATH="$DOTPATH" python3 -c "
import yaml, sys, os
try:
    with open(os.environ['_VAULT_FILE']) as f:
        data = yaml.safe_load(f) or {}
    keys = os.environ['_DOTPATH'].split('.')
    val = data
    for k in keys:
        val = val[k]
    print(val)
except (KeyError, TypeError):
    print(f'[secret] key not found: {os.environ[\"_DOTPATH\"]}', file=sys.stderr)
    sys.exit(1)
"
        ;;

    set)
        [ $# -lt 3 ] && usage
        DOTPATH="$2"
        VALUE="$3"
        _VAULT_FILE="$VAULT_FILE" _DOTPATH="$DOTPATH" _VALUE="$VALUE" python3 -c "
import yaml, os
with open(os.environ['_VAULT_FILE']) as f:
    data = yaml.safe_load(f) or {}
keys = os.environ['_DOTPATH'].split('.')
d = data
for k in keys[:-1]:
    d = d.setdefault(k, {})
d[keys[-1]] = os.environ['_VALUE']
with open(os.environ['_VAULT_FILE'], 'w') as f:
    yaml.dump(data, f, default_flow_style=False)
print(f'[secret] stored: {os.environ[\"_DOTPATH\"]}')
"
        chmod 600 "$VAULT_FILE"
        ;;

    list)
        _VAULT_FILE="$VAULT_FILE" python3 -c "
import yaml, os
with open(os.environ['_VAULT_FILE']) as f:
    data = yaml.safe_load(f) or {}
for section, values in sorted(data.items()):
    if isinstance(values, dict):
        for key in sorted(values.keys()):
            print(f'{section}.{key}')
    else:
        print(section)
"
        ;;

    delete)
        [ $# -lt 2 ] && usage
        DOTPATH="$2"
        _VAULT_FILE="$VAULT_FILE" _DOTPATH="$DOTPATH" python3 -c "
import yaml, sys, os
with open(os.environ['_VAULT_FILE']) as f:
    data = yaml.safe_load(f) or {}
keys = os.environ['_DOTPATH'].split('.')
d = data
for k in keys[:-1]:
    if k not in d:
        print(f'[secret] key not found: {os.environ[\"_DOTPATH\"]}', file=sys.stderr)
        sys.exit(1)
    d = d[k]
if keys[-1] in d:
    del d[keys[-1]]
    # clean up empty parent
    if len(keys) > 1:
        parent = data
        for k in keys[:-2]:
            parent = parent[k]
        if isinstance(parent.get(keys[-2]), dict) and not parent[keys[-2]]:
            del parent[keys[-2]]
    with open(os.environ['_VAULT_FILE'], 'w') as f:
        yaml.dump(data, f, default_flow_style=False)
    print(f'[secret] deleted: {os.environ[\"_DOTPATH\"]}')
else:
    print(f'[secret] key not found: {os.environ[\"_DOTPATH\"]}', file=sys.stderr)
    sys.exit(1)
"
        chmod 600 "$VAULT_FILE"
        ;;

    *)
        usage
        ;;
esac
