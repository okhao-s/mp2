import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SITES_DIR = PROJECT_ROOT / "plugins.v2" / "autosigninenhanced" / "sites"


def test_enhanced_site_modules_do_not_import_legacy_base_handler():
    legacy_importers = []

    for site_module in sorted(SITES_DIR.glob("*.py")):
        module_ast = ast.parse(
            site_module.read_text(encoding="utf-8"),
            filename=str(site_module),
        )
        for node in ast.walk(module_ast):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "app.plugins.autosignin.sites"
                and any(alias.name == "_ISiteSigninHandler" for alias in node.names)
            ):
                legacy_importers.append(site_module.name)
                break

    assert legacy_importers == [], (
        "AutoSignInEnhanced site modules import the legacy base handler: "
        + ", ".join(legacy_importers)
    )
