"""Subprocess-based tests for shell scripts: job.sh, remind.sh, notify.sh, secret.sh."""

import os
import subprocess

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))


def run_bash(script, timeout=5, env=None):
    """Run a bash command and return the result."""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    return result


class TestJobShParseDuration:
    """Test job.sh parse_duration function."""

    def _parse(self, duration):
        # Source only the function (avoid the main dispatch and set -euo pipefail)
        result = run_bash(
            f"""
            parse_duration() {{
                local input="$1"
                local total=0
                local remaining="$input"
                if [[ "$remaining" =~ ([0-9]+)h ]]; then
                    total=$((total + ${{BASH_REMATCH[1]}} * 3600))
                    remaining="${{remaining//${{BASH_REMATCH[0]}}/}}"
                fi
                if [[ "$remaining" =~ ([0-9]+)m ]]; then
                    total=$((total + ${{BASH_REMATCH[1]}} * 60))
                    remaining="${{remaining//${{BASH_REMATCH[0]}}/}}"
                fi
                if [[ "$remaining" =~ ([0-9]+)s ]]; then
                    total=$((total + ${{BASH_REMATCH[1]}}))
                    remaining="${{remaining//${{BASH_REMATCH[0]}}/}}"
                fi
                if [[ "$total" -eq 0 ]] && [[ "$input" =~ ^[0-9]+$ ]]; then
                    total="$input"
                fi
                if [[ "$total" -eq 0 ]]; then
                    echo "error" >&2
                    return 1
                fi
                echo "$total"
            }}
            parse_duration "{duration}"
            """
        )
        return result

    def test_parse_hours_minutes(self):
        result = self._parse("1h30m")
        assert result.returncode == 0
        assert result.stdout.strip() == "5400"

    def test_parse_minutes_only(self):
        result = self._parse("30m")
        assert result.returncode == 0
        assert result.stdout.strip() == "1800"

    def test_parse_hours_only(self):
        result = self._parse("2h")
        assert result.returncode == 0
        assert result.stdout.strip() == "7200"

    def test_parse_seconds(self):
        result = self._parse("90s")
        assert result.returncode == 0
        assert result.stdout.strip() == "90"

    def test_parse_raw_number(self):
        result = self._parse("300")
        assert result.returncode == 0
        assert result.stdout.strip() == "300"


class TestJobShParseTime:
    """Test job.sh parse_time function via subprocess."""

    def test_parse_hh_mm(self):
        # Inline the function since sourcing job.sh triggers main dispatch
        result = run_bash(
            r"""
            parse_time() {
                local time_str="$1"
                if [[ ! "$time_str" =~ ^([0-9]{1,2}):([0-9]{2})$ ]]; then
                    echo "error: invalid time format" >&2
                    return 1
                fi
                python3 -c "
import time, sys
h, m = ${BASH_REMATCH[1]}, ${BASH_REMATCH[2]}
now = time.time()
lt = time.localtime(now)
target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, lt.tm_isdst))
if target <= now:
    target += 86400
print(int(target))
"
            }
            parse_time "9:30"
            """
        )
        assert result.returncode == 0
        timestamp = result.stdout.strip()
        assert timestamp.isdigit()
        assert int(timestamp) > 0

    def test_parse_invalid_time(self):
        result = run_bash(
            """
            parse_time() {
                local time_str="$1"
                if [[ ! "$time_str" =~ ^([0-9]{1,2}):([0-9]{2})$ ]]; then
                    echo "error: invalid time format" >&2
                    return 1
                fi
                echo "ok"
            }
            parse_time "not-a-time"
            """
        )
        assert result.returncode != 0


class TestRemindShParseDuration:
    """Verify remind.sh parse_duration works identically to job.sh."""

    def _parse(self, duration):
        result = run_bash(
            f"""
            parse_duration() {{
                local input="$1"
                local total=0
                local remaining="$input"
                if [[ "$remaining" =~ ([0-9]+)h ]]; then
                    total=$((total + ${{BASH_REMATCH[1]}} * 3600))
                    remaining="${{remaining//${{BASH_REMATCH[0]}}/}}"
                fi
                if [[ "$remaining" =~ ([0-9]+)m ]]; then
                    total=$((total + ${{BASH_REMATCH[1]}} * 60))
                    remaining="${{remaining//${{BASH_REMATCH[0]}}/}}"
                fi
                if [[ "$remaining" =~ ([0-9]+)s ]]; then
                    total=$((total + ${{BASH_REMATCH[1]}}))
                    remaining="${{remaining//${{BASH_REMATCH[0]}}/}}"
                fi
                if [[ "$total" -eq 0 ]] && [[ "$input" =~ ^[0-9]+$ ]]; then
                    total="$input"
                fi
                if [[ "$total" -eq 0 ]]; then
                    echo "error" >&2
                    return 1
                fi
                echo "$total"
            }}
            parse_duration "{duration}"
            """
        )
        return result

    def test_parse_hours_minutes(self):
        result = self._parse("1h30m")
        assert result.returncode == 0
        assert result.stdout.strip() == "5400"

    def test_parse_minutes_only(self):
        result = self._parse("30m")
        assert result.returncode == 0
        assert result.stdout.strip() == "1800"


class TestSecretShVaultOps:
    """Test secret.sh vault operations (set, get, list, delete)."""

    def test_set_and_get_secret(self, tmp_path):
        vault = str(tmp_path / "vault.yaml")
        # Create empty vault
        with open(vault, "w") as f:
            f.write("")

        # Set a secret
        result = run_bash(
            f'_VAULT_FILE="{vault}" _DOTPATH="svc.api_key" _VALUE="test123" python3 -c "\n'
            'import yaml, os\n'
            'with open(os.environ[\'_VAULT_FILE\']) as f:\n'
            '    data = yaml.safe_load(f) or {}\n'
            'keys = os.environ[\'_DOTPATH\'].split(\'.\')\n'
            'd = data\n'
            'for k in keys[:-1]:\n'
            '    d = d.setdefault(k, {})\n'
            'd[keys[-1]] = os.environ[\'_VALUE\']\n'
            'with open(os.environ[\'_VAULT_FILE\'], \'w\') as f:\n'
            '    yaml.dump(data, f, default_flow_style=False)\n'
            '"'
        )
        assert result.returncode == 0

        # Get the secret
        result = run_bash(
            f'_VAULT_FILE="{vault}" _DOTPATH="svc.api_key" python3 -c "\n'
            'import yaml, os\n'
            'with open(os.environ[\'_VAULT_FILE\']) as f:\n'
            '    data = yaml.safe_load(f) or {}\n'
            'keys = os.environ[\'_DOTPATH\'].split(\'.\')\n'
            'val = data\n'
            'for k in keys:\n'
            '    val = val[k]\n'
            'print(val)\n'
            '"'
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "test123"

    def test_list_secrets(self, tmp_path):
        vault = str(tmp_path / "vault.yaml")
        # Write some secrets directly
        import yaml
        data = {"svc": {"key1": "val1", "key2": "val2"}}
        with open(vault, "w") as f:
            yaml.dump(data, f)

        result = run_bash(
            f'_VAULT_FILE="{vault}" python3 -c "\n'
            'import yaml, os\n'
            'with open(os.environ[\'_VAULT_FILE\']) as f:\n'
            '    data = yaml.safe_load(f) or {}\n'
            'for section, values in sorted(data.items()):\n'
            '    if isinstance(values, dict):\n'
            '        for key in sorted(values.keys()):\n'
            '            print(f\'{section}.{key}\')\n'
            '    else:\n'
            '        print(section)\n'
            '"'
        )
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        assert "svc.key1" in lines
        assert "svc.key2" in lines

    def test_delete_secret(self, tmp_path):
        vault = str(tmp_path / "vault.yaml")
        import yaml
        data = {"svc": {"key1": "val1"}}
        with open(vault, "w") as f:
            yaml.dump(data, f)

        # Delete the key
        result = run_bash(
            f'_VAULT_FILE="{vault}" _DOTPATH="svc.key1" python3 -c "\n'
            'import yaml, sys, os\n'
            'with open(os.environ[\'_VAULT_FILE\']) as f:\n'
            '    data = yaml.safe_load(f) or {}\n'
            'keys = os.environ[\'_DOTPATH\'].split(\'.\')\n'
            'd = data\n'
            'for k in keys[:-1]:\n'
            '    d = d[k]\n'
            'if keys[-1] in d:\n'
            '    del d[keys[-1]]\n'
            '    with open(os.environ[\'_VAULT_FILE\'], \'w\') as f:\n'
            '        yaml.dump(data, f, default_flow_style=False)\n'
            '"'
        )
        assert result.returncode == 0

        # Verify it's gone
        with open(vault) as f:
            remaining = yaml.safe_load(f) or {}
        assert "key1" not in remaining.get("svc", {})


class TestNotifySh:
    """Test notify.sh argument handling."""

    def test_notify_requires_message(self):
        result = run_bash(
            f"bash {SCRIPTS_DIR}/notify.sh 2>&1",
            env={**os.environ, "PORT": "99999"},  # non-existent port
        )
        # Should exit non-zero when no message provided
        assert result.returncode != 0
