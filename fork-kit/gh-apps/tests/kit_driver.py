"""Test driver: runs one gh-apps module's main() in a fresh Python with a fake host (tests/kit_fixture.py drive()).
The only changes are module attributes, set here; the code under test reads no test switch of its own."""
import importlib, json, os, sys

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KIT, 'lib'))

from ghapps import common as c  # noqa: E402

spec = json.loads(sys.argv[1])
c.PREFIX, c.ROOT_UID = spec['prefix'], spec['root_uid']
c.FAKE_IDS = {k: tuple(v) for k, v in spec['ids'].items()}
module = importlib.import_module('ghapps.' + spec['module'])
for dotted, value in spec['extra'].items():
    mod, _, attr = dotted.rpartition('.')
    setattr(importlib.import_module('ghapps.' + mod), attr, value)

from ghapps import run  # noqa: E402

run.mint_command = lambda name: [sys.executable, '-I', '-c', f'print({spec["token"]!r})']
sys.exit(module.main(spec['args']))
