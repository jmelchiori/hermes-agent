#!/bin/sh
set -eu
cp /hermeswebui_init.bash /tmp/hermeswebui_init.bash
python3 - <<'PY'
from pathlib import Path
path = Path('/tmp/hermeswebui_init.bash')
text = path.read_text()
needle = 'uv pip install mcp --trusted-host pypi.org --trusted-host files.pythonhosted.org'
anchor = 'uv pip install -U pip setuptools --trusted-host pypi.org --trusted-host files.pythonhosted.org'
if needle not in text:
    if anchor not in text:
        raise SystemExit('Expected pip/setuptools install line not found in init script copy')
    text = text.replace(anchor, anchor + '\n  ' + needle, 1)
    path.write_text(text)
PY
chmod +x /tmp/hermeswebui_init.bash
rm -f /app/venv/.deps_installed || true
exec /tmp/hermeswebui_init.bash
