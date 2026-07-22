import importlib.util
import json
import sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[2]
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec); assert spec.loader
    sys.modules[name] = module; spec.loader.exec_module(module); return module
BUILD, INSTALL = load("build_asset_bundle"), load("install_assets")
def point_build_at(monkeypatch, root):
    monkeypatch.setattr(BUILD, "ROOT", root.parent)
    monkeypatch.setattr(BUILD, "ASSET_DIR", root)


def tree(tmp_path, pipeline=True, component=True, name="metrics-rigsignal.cpu.json"):
    root = tmp_path / "elastic"
    for d in ("component-templates", "index-templates", "pipelines", "transforms"): (root / d).mkdir(parents=True)
    if component: (root / "component-templates" / "metrics-rigsignal.cpu@package.json").write_text("{}")
    if pipeline: (root / "pipelines" / "metrics-rigsignal.cpu-0.5.0.json").write_text("{}")
    (root / "index-templates" / name).write_text(json.dumps({"composed_of":["metrics-rigsignal.cpu@package"],"template":{"settings":{"index":{"default_pipeline":"metrics-rigsignal.cpu-0.5.0"}}}})); return root
def test_missing_referenced_pipeline_fails_build(monkeypatch, tmp_path):
    point_build_at(monkeypatch, tree(tmp_path, pipeline=False))
    with pytest.raises(BUILD.BundleError, match="metrics-rigsignal.cpu-0.5.0"): BUILD.validate_dependencies(BUILD.read_assets())
def test_missing_composed_of_component_fails_build(monkeypatch, tmp_path):
    point_build_at(monkeypatch, tree(tmp_path, component=False))
    with pytest.raises(BUILD.BundleError, match="metrics-rigsignal.cpu@package"): BUILD.validate_dependencies(BUILD.read_assets())
def test_non_conforming_filename_fails_build(monkeypatch, tmp_path):
    point_build_at(monkeypatch, tree(tmp_path, name="bad name.json"))
    with pytest.raises(BUILD.BundleError, match="invalid asset filename"): BUILD.read_assets()
def test_installer_failure_table_and_nonzero(monkeypatch, capsys, tmp_path):
    bundle = INSTALL.Bundle("0.3.1", "test", [INSTALL.Asset("pipelines", "broken", "elastic/pipelines/broken.json", b"{}")])
    monkeypatch.setattr(INSTALL, "load_bundle", lambda _: bundle)
    monkeypatch.setenv("RIGSIGNAL_ES_URL", "http://es"); monkeypatch.setenv("RIGSIGNAL_KB_URL", "http://kb"); monkeypatch.setenv("RIGSIGNAL_ES_AUTH", "elastic:pw")
    def fail(_): raise INSTALL.urllib.error.URLError("offline")
    monkeypatch.setattr(INSTALL.urllib.request, "urlopen", fail)
    monkeypatch.setattr("sys.argv", ["install_assets.py", "--bundle", str(tmp_path / "x.tgz")])
    assert INSTALL.main() == 1
    assert "pipelines | broken | network error: offline" in capsys.readouterr().err
def test_transform_update_strips_pivot(monkeypatch, tmp_path):
    asset = INSTALL.Asset("transforms", "t", "elastic/transforms/t.json", b'{"pivot":{},"description":"x"}')
    monkeypatch.setattr(INSTALL, "load_bundle", lambda _: INSTALL.Bundle("0.3.1", "test", [asset]))
    monkeypatch.setenv("RIGSIGNAL_ES_URL", "http://es"); monkeypatch.setenv("RIGSIGNAL_KB_URL", "http://kb"); monkeypatch.setenv("RIGSIGNAL_ES_AUTH", "elastic:pw")
    calls=[]
    def request(base, path, method, auth, data=None, headers=None): calls.append((path, method, data)); return b"{}"
    monkeypatch.setattr(INSTALL, "request", request); monkeypatch.setattr("sys.argv", ["install_assets.py", "--bundle", str(tmp_path / "x.tgz")])
    assert INSTALL.main() == 0
    assert "pivot" not in json.loads(next(data for path, method, data in calls if path.endswith("/_update")))
