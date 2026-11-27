"""kit_load: the heavy kit's one module loader (#800 item 5).

A kit file loads another kit file with load(name, rel, __file__): rel ('lib/capacity_rule.py',
'common/machine_file.py') is looked up in the kit that holds the asking file (the installed version folder), then
next to that kit in the repository (fork-kit/<rel>), so a copy of the kit (a test's red-check copy, an older release)
always loads its own files. Each module comes fresh (not shared through sys.modules). The asking file finds this one
by putting its own lib folder first on sys.path (it runs with -I, so nothing else is there):

    sys.path[:0] = [] if LIB in sys.path else [LIB]
    import kit_load
"""
import importlib.machinery, importlib.util, os


def kit_of(near):
    """The kit folder of a kit file (its bin/, lib/ or sbin/ is one level down), links resolved."""
    return os.path.dirname(os.path.dirname(os.path.realpath(near)))


def load(name, rel, near):
    """The module rel of the kit that holds the file near, loaded under name; ImportError when it is in neither
    place."""
    kit = kit_of(near)
    for path in (os.path.join(kit, rel), os.path.join(os.path.dirname(kit), rel)):
        if os.path.isfile(path):
            loader = importlib.machinery.SourceFileLoader(name, path)
            mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
            loader.exec_module(mod)
            return mod
    raise ImportError(f'{rel} not found in {kit} or next to it')
