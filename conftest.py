"""Make the vendored `homeaccess` library importable in tests/dev without an
editable install. The library lives inside the HA component (so it ships with
HACS); this puts that directory on sys.path so `import homeaccess` resolves.
"""
import sys
from pathlib import Path

_VENDORED = Path(__file__).parent / "custom_components" / "philips_home_access"
sys.path.insert(0, str(_VENDORED))
