import importlib
import pkgutil


class ModuleHelper:
    @staticmethod
    def load(package_name, filter_func=None):
        try:
            pkg = importlib.import_module(package_name)
        except Exception:
            return []
        out = []
        for _, name, _ in pkgutil.iter_modules(pkg.__path__):
            try:
                mod = importlib.import_module(f"{package_name}.{name}")
            except Exception:
                continue
            for attr in dir(mod):
                obj = getattr(mod, attr)
                if isinstance(obj, type) and filter_func and filter_func(name, obj):
                    out.append(obj)
        return out
