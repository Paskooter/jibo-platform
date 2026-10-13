"""Public-source-only release gates; never load robot assets or live config."""
from pathlib import Path
import importlib.util
import io
import shutil
import tarfile
import json
import re
import subprocess
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


def load_plugin():
    name = "jibo_public_release_tests"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]

ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = {
    "httpx==0.28.1", "websockets==15.0.1", "PyYAML==6.0.3",
    "fastapi==0.133.1", "Pillow==12.3.0", "aiohttp==3.14.3",
}


# Reviewed publication allowlist. Never derive it from a directory glob.
PUBLIC_FILES = frozenset("""
.github/workflows/ci.yml
.gitignore
CHANGELOG.md
LICENSE
NOTICE
README.md
SECURITY.md
__init__.py
adapter.py
agent-skills/jibo-hermes-integration/SKILL.md
agent-skills/jibo-hermes-integration/references/channel-response-contract.md
agent-skills/jibo-hermes-integration/references/hermes-platform-adapter.md
agent-skills/jibo-hermes-integration/references/native-look-capture.md
agent-skills/jibo-hermes-integration/references/pairing-and-auth.md
asr.py
catalog.py
catalog_tool.py
dashboard/__init__.py
dashboard/dist/index.js
dashboard/dist/style.css
dashboard/manifest.json
dashboard/plugin_api.py
device_status.py
esml.py
image_tool.py
media.py
model_settings.py
native_adapter.py
native_protocol.py
native_tools.py
observation.py
plugin.yaml
pronunciation.py
rom.py
scripts/set-release-sha.py
status.py
tests/__init__.py
tests/test_dashboard_adapter_identity.py
tests/test_device_status.py
tests/test_esml.py
tests/test_expression_catalog.py
tests/test_final_delivery.py
tests/test_follow_up.py
tests/test_gateway_reply_handoff.py
tests/test_image_display.py
tests/test_model_settings.py
tests/test_native_poc.py
tests/test_native_startup.py
tests/test_observation.py
tests/test_observation_vision.py
tests/test_pairing_display.py
tests/test_pairing_safety.py
tests/test_phoenix_setup.py
tests/test_prompt_refresh.py
tests/test_public_release.py
tests/test_release_sha.py
tests/test_speech_pipeline.py
tests/test_tap_cancel.py
tests/test_tool_boundary.py
tests/test_transport_compat.py
tests/test_turn_cancellation.py
tests/test_turn_ingest_http.py
tests/test_turn_record.py
tests/test_turn_spine.py
tests/test_turn_stream.py
tests/test_web_panel_tool.py
tool.py
tools/refresh_platform_hint.py
turn_ingest.py
turn_record.py
turn_stream.py
""".splitlines()) - {""}
HOST_ADDITIONS = frozenset({"tests/test_phoenix_setup.py", "tests/test_pairing_safety.py"})


def source_files(root=ROOT):
    if (root / ".git").exists():
        return set(subprocess.check_output(
            ["git", "ls-files", "-z"], cwd=root, text=True,
        ).split("\0")) - {""}
    return {str(path.relative_to(root)) for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts}


def validate_members(files, symlinks=(), *, integrated=False):
    required = PUBLIC_FILES if integrated else PUBLIC_FILES - HOST_ADDITIONS
    if set(files) - PUBLIC_FILES:
        raise ValueError("unapproved publication files: " + str(sorted(set(files) - PUBLIC_FILES)))
    if required - set(files):
        raise ValueError("missing publication files: " + str(sorted(required - set(files))))
    if symlinks:
        raise ValueError("symlinks are not public source files: " + str(sorted(symlinks)))


class PublicArtifactTests(unittest.TestCase):
    def test_source_population_is_the_reviewed_allowlist(self):
        files = source_files()
        try:
            validate_members(files, [f for f in files if (ROOT / f).is_symlink()],
                             integrated=os.getenv("JIBO_RELEASE_INTEGRATED") == "1")
        except ValueError as exc:
            self.fail(str(exc))
        self.assertEqual(len(PUBLIC_FILES), 71)
        self.assertEqual(len(PUBLIC_FILES - HOST_ADDITIONS), 69)

    def test_source_archive_has_only_approved_regular_files(self):
        if not (ROOT / ".git").exists():
            self.skipTest("git archive requires the source checkout")
        data = subprocess.check_output(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            entries = [member for member in archive.getmembers() if not member.isdir()]
            self.assertTrue(all(member.isfile() for member in entries))
            self.assertEqual(len(entries), len({m.name for m in entries}))
            try:
                validate_members([m.name for m in entries],
                                 integrated=os.getenv("JIBO_RELEASE_INTEGRATED") == "1")
            except ValueError as exc:
                self.fail(str(exc))
            for member in entries:
                self.assertEqual(archive.extractfile(member).read(), (ROOT / member.name).read_bytes(),
                                 "archive differs from tested source: " + member.name)

    def test_gate_rejects_unapproved_files_and_symlinks(self):
        for extra in ("animdb.json", "expression_vocabulary.json", "config.yaml",
                      "auth.json", ".env", "deploy.sh", "private/history.txt",
                      "be/runtime.js", "node_modules/sdk.js", "__pycache__/cached.pyc"):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "unapproved"):
                validate_members(PUBLIC_FILES | {extra})
        with self.assertRaisesRegex(ValueError, "symlinks"):
            validate_members(PUBLIC_FILES, ["adapter.py"])

    def test_source_text_has_no_developer_paths_or_household_targets(self):
        forbidden = re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+|"
                               r"\b(?:" + "m" + "oth|a" + "ero" + r")(?:\b|[-.])|"
                               "hermes-" + "plugins-staging|phoenix-" + "private|jibo" + "-be|hermes" + "-be", re.I)
        for relative in source_files() & PUBLIC_FILES:
            with self.subTest(file=relative):
                self.assertIsNone(forbidden.search((ROOT / relative).read_text()), relative)

    def test_selected_guide_links_resolve_inside_the_public_artifact(self):
        guide = ROOT / "agent-skills/jibo-hermes-integration"
        for path in guide.rglob("*.md"):
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text()):
                if "://" not in target and not target.startswith("#"):
                    resolved = (path.parent / target.split("#", 1)[0]).resolve()
                    self.assertTrue(resolved.is_relative_to(ROOT))
                    self.assertTrue(resolved.is_file(), (path.name, target))
                    self.assertIn(str(resolved.relative_to(ROOT)), PUBLIC_FILES)

    def test_licence_notice_source_url_and_install_pin_slots_are_explicit(self):
        manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
        self.assertEqual(manifest["homepage"], "https://github.com/Paskooter/jibo-platform")
        licence = (ROOT / "LICENSE").read_text()
        self.assertIn("BSD 3-Clause License", licence)
        self.assertIn("Copyright (c) 2026, pasketti", licence)
        notice = (ROOT / "NOTICE").read_text()
        self.assertIn("MIT License", notice)
        self.assertIn("Hermes Agent", notice)
        readme = (ROOT / "README.md").read_text()
        token = "PLUGIN_" + "RELEASE_SHA"
        install = re.search(
            r"hermes plugins install https://github\.com/Paskooter/jibo-platform --ref "
            r"(" + token + r"|[0-9a-fA-F]{40}) --enable(?:\s|$)", readme,
        )
        self.assertIsNotNone(install, "install needs the single release token or a full immutable SHA")
        assert install is not None
        locations = {}
        for relative in source_files():
            data = (ROOT / relative).read_bytes()
            count = data.count(token.encode("ascii"))
            if count:
                locations[relative] = count
            self.assertIsNone(re.search(rb"<(?:REVIEWED|QUALIFIED|NEW_REVIEWED)_[A-Z0-9_]+>", data),
                              "obsolete release placeholder: " + relative)
        if install.group(1) == token:
            self.assertNotEqual(os.getenv("JIBO_RELEASE_PUBLISHED"), "1",
                                "published install instructions cannot contain a release token")
            self.assertEqual(locations, {"README.md": 1}, "only one documented release token is allowed")
        else:
            self.assertNotEqual(install.group(1), "0" * 40)
            self.assertEqual(locations, {}, "no token may remain after filling the release SHA")
        self.assertIn("never expose it to the Internet", readme)
        self.assertIn("Tested with Hermes 0.21.1", readme)
        self.assertIn("6494163911460844c73ac76abb5aaa4f967ae14b", readme)

    @unittest.skipUnless(os.getenv("JIBO_RELEASE_INTEGRATED") == "1", "requires integrated HOST candidate")
    def test_integrated_dashboard_cache_version_matches_plugin(self):
        manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
        dashboard = json.loads((ROOT / "dashboard/manifest.json").read_text())
        self.assertEqual(dashboard["version"], manifest["version"])
        self.assertNotIn(dashboard.get("icon"), ("Bot", "Robot", "Eye"))
        self.assertIn("?hermes_v=" + manifest["version"], dashboard["entry"])
        self.assertIn("?hermes_v=" + manifest["version"], dashboard["css"])


class PublicReleaseSHAStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jibo-pin-gate-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("README.md", "NOTICE", "LICENSE", "plugin.yaml"):
            shutil.copyfile(ROOT / name, self.root / name)
        self.token = "PLUGIN_" + "RELEASE_SHA"
        self.readme = self.root / "README.md"
        text = self.readme.read_text()
        # The same tests run unchanged before and after the release script.
        text = re.sub(r"(?<=--ref )(?:[0-9a-fA-F]{40}|" + self.token + r")(?= --enable)",
                      self.token, text, count=1)
        self.readme.write_text(text)

    def check_gate(self, published=False):
        with patch.dict(os.environ, {"JIBO_RELEASE_PUBLISHED": "1" if published else "0"}), \
             patch.object(sys.modules[__name__], "ROOT", self.root), \
             patch.object(sys.modules[__name__], "source_files", return_value={p.name for p in self.root.iterdir()}):
            PublicArtifactTests("test_licence_notice_source_url_and_install_pin_slots_are_explicit").test_licence_notice_source_url_and_install_pin_slots_are_explicit()

    def test_gate_accepts_exactly_one_documented_prepublication_token(self):
        self.check_gate()

    def test_gate_accepts_a_filled_sha_and_requires_no_token_after_publication(self):
        self.readme.write_text(self.readme.read_text().replace(self.token, "ab12cd34" * 5))
        self.check_gate(published=True)
        self.check_gate()

    def test_gate_rejects_duplicate_or_misplaced_tokens(self):
        original = self.readme.read_text()
        self.readme.write_text(original + "\n" + self.token)
        with self.assertRaises(AssertionError):
            self.check_gate()
        self.readme.write_text(original)
        (self.root / "other.md").write_text(self.token)
        with self.assertRaises(AssertionError):
            self.check_gate()

    def test_published_gate_rejects_the_prepublication_token(self):
        with self.assertRaises(AssertionError):
            self.check_gate(published=True)


class PublicDocumentationTests(unittest.TestCase):
    def test_requirements_name_the_tested_runtime_and_home_network_app(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("Tested with Hermes 0.21.1", readme)
        self.assertIn("6494163911460844c73ac76abb5aaa4f967ae14b", readme)
        self.assertIn("minimum version is **0.21.1**", readme)
        self.assertIn("Agent Mode app 1.0.1 or newer", readme)
        self.assertIn("Python 3.11", readme)
        self.assertIn('"$HOME/.hermes/bin/uv" pip install --python "$HOME/.hermes/hermes-agent/venv/bin/python"', readme)

    def test_user_docs_do_not_expose_internal_review_status(self):
        files = ["README.md", "SECURITY.md", "NOTICE", "plugin.yaml"]
        files += [str(p.relative_to(ROOT)) for p in (ROOT / "agent-skills").rglob("*.md")]
        forbidden = re.compile(r"\b(?:candidate|qualified|qualification|root|lane|pending|INFERRED|UNKNOWN)\b|not a published release", re.I)
        for relative in files:
            with self.subTest(file=relative):
                text = (ROOT / relative).read_text()
                self.assertIsNone(forbidden.search(text), relative)
        self.assertNotIn("ssh -N", (ROOT / "README.md").read_text())

    def test_security_reporting_uses_github_private_vulnerability_reports(self):
        security = (ROOT / "SECURITY.md").read_text()
        self.assertIn("Security tab", security)
        self.assertIn("Report a vulnerability", security)
        self.assertIn("https://github.com/Paskooter/jibo-platform/security/advisories/new", security)
        self.assertIn("public issues", security)
        for detail in ("codes", "tokens", "addresses", "recordings", "photos"):
            self.assertIn(detail, security)

    def test_changelog_and_removal_describe_user_visible_behavior(self):
        self.assertTrue((ROOT / "CHANGELOG.md").is_file(), "0.3.1 changelog is required")
        changelog = (ROOT / "CHANGELOG.md").read_text()
        self.assertIn("## 0.3.1", changelog)
        readme = (ROOT / "README.md").read_text()
        self.assertIn("does not unpair Jibo", readme)
        self.assertIn("Reset pairing", readme)
        self.assertIn("Remove the app and its data", readme)
        self.assertIn("Remove the app, keep its settings", readme)
        self.assertIn("process", readme)
        self.assertIn("Camera access is off", readme)
        self.assertNotIn("Start with no computer tools", readme)


class PublicCITests(unittest.TestCase):
    def test_ci_collects_pytest_cases_with_their_sibling_imports(self):
        workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["source-only"]["steps"]
        commands = "\n".join(step.get("run", "") for step in steps)
        self.assertIn("pytest==9.1.1", commands)
        self.assertIn("-m pytest tests", commands)
        self.assertIn('PYTHONPATH="$PWD/tests:$PWD/.verification/core"', commands)
        self.assertNotIn("pytest-asyncio", commands)

    def test_ci_defaults_to_the_exact_tested_public_hermes_commit(self):
        workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow.get("env", {}).get("HERMES_REF"), "6494163911460844c73ac76abb5aaa4f967ae14b")
        self.assertIn("push", workflow["on"])
        self.assertIn("pull_request", workflow["on"])


class PublicManifestTests(unittest.TestCase):
    def test_manifest_bounds_the_tested_hermes_api_version(self):
        from importlib.metadata import version
        manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
        self.assertEqual(manifest.get("requires_hermes"), "==0.21.1")
        self.assertEqual(version("hermes-agent"), "0.21.1")

    def test_manifest_has_proposed_license_version_and_host_dependencies(self):
        manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
        self.assertEqual(manifest["license"], "BSD-3-Clause")
        self.assertEqual(manifest["version"], "0.3.1")
        self.assertEqual(set(manifest["python_dependencies"]), DEPENDENCIES)
        self.assertEqual(manifest["kind"], "platform")
        self.assertEqual(manifest["requires_env"], [])


class PublicTargetTests(unittest.TestCase):
    def test_legacy_rom_requires_an_explicit_target_before_creating_clients(self):
        from gateway.config import PlatformConfig
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import parse_manifest_file
        plugin = load_plugin()
        manifest = parse_manifest_file(ROOT / "plugin.yaml", ROOT, "user", "")
        manager = PluginManager()
        plugin.register(PluginContext(manifest, manager))
        self.addCleanup(manager.unload)
        from jibo_public_release_tests.adapter import JiboRomAdapter
        with patch.dict(os.environ, {"JIBO_HOST": ""}), \
             patch("jibo_public_release_tests.adapter.RomSession") as rom:
            with self.assertRaisesRegex(ValueError, "host|target"):
                JiboRomAdapter(PlatformConfig(extra={"mode": "rom"}))
            rom.assert_not_called()


class PublicCatalogTests(unittest.TestCase):
    def test_curated_schema_labels_raw_animation_data_as_not_bundled(self):
        load_plugin()
        from jibo_public_release_tests import native_tools
        self.assertIn("not bundled", native_tools.JIBO_EXPRESS_SCHEMA["description"])

    def test_curated_expression_dispatch_works_without_generated_catalogs(self):
        from types import SimpleNamespace
        load_plugin()
        from jibo_public_release_tests import native_tools
        self.assertFalse((ROOT / "expression_vocabulary.json").exists())
        self.assertFalse((ROOT / "animdb.json").exists())
        native_tools._EXPRESSION_KEYS_CACHE = None
        self.assertEqual(native_tools._expression_keys(), [])
        self.assertNotIn("enum", native_tools._express_schema()["parameters"]["properties"]["expression"])
        calls = []

        class SyntheticAuthorizedPeer:
            def resolve_device_id(self):
                return "synthetic-robot"

            def native_call(self, method, params, **kwargs):
                calls.append((method, dict(params), kwargs))
                if params["key"] != "synthetic_greeting":
                    return SimpleNamespace(success=False, error="supported key: synthetic_greeting",
                                           raw_response={"code": "UNSUPPORTED_EXPRESSION"})
                return SimpleNamespace(success=True, raw_response={"key": "synthetic_greeting"})

        with patch.object(native_tools, "get_active_adapter", return_value=SyntheticAuthorizedPeer()):
            result = json.loads(native_tools.jibo_express_tool({"expression": "synthetic_greeting"}, platform="jibo"))
            refused = json.loads(native_tools.jibo_express_tool({"expression": "synthetic_unknown"}, platform="jibo"))
        self.assertTrue(result["success"])
        self.assertEqual(result["result"]["key"], "synthetic_greeting")
        self.assertFalse(refused["success"])
        self.assertIn("synthetic_greeting", refused["error"])
        self.assertEqual(calls[0][0], "expression.perform")
        self.assertEqual(calls[0][2]["required_capability"], "expression.perform")

    def test_missing_catalog_disables_raw_catalog_tool_advertising(self):
        load_plugin()
        from jibo_public_release_tests import catalog_tool
        with patch.object(catalog_tool, "load_catalog", return_value=[]):
            self.assertFalse(catalog_tool.jibo_find_expression_available())
            self.assertFalse(catalog_tool.jibo_play_expression_available())
        with patch.object(catalog_tool, "load_catalog", return_value=[{"name": "synthetic"}]):
            self.assertTrue(catalog_tool.jibo_find_expression_available())
            self.assertTrue(catalog_tool.jibo_play_expression_available())

    def test_missing_catalog_never_opens_an_implicit_external_location(self):
        load_plugin()
        from jibo_public_release_tests import catalog
        catalog.load_catalog.cache_clear()
        attempted = []

        def missing(path, *args, **kwargs):
            attempted.append(path)
            raise FileNotFoundError(path)

        with patch.object(Path, "open", missing):
            self.assertEqual(catalog.load_catalog(), [])
        self.assertEqual(attempted, [ROOT / "animdb.json"])
        catalog.load_catalog.cache_clear()


class PublicDependencyTests(unittest.TestCase):
    def test_existing_ingest_surface_declares_and_checks_its_http_dependency(self):
        manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
        self.assertIn("aiohttp==3.14.3", manifest["python_dependencies"])
        import builtins
        plugin = load_plugin()
        real_import = builtins.__import__
        def blocked(name, *args, **kwargs):
            if name == "aiohttp":
                raise ImportError("synthetic missing ingest dependency")
            return real_import(name, *args, **kwargs)
        with patch.object(builtins, "__import__", blocked):
            self.assertFalse(plugin.check_requirements())

    def test_requirement_probe_checks_every_declared_host_surface(self):
        import builtins
        plugin = load_plugin()
        real_import = builtins.__import__
        for missing in ("httpx", "websockets", "yaml", "fastapi", "PIL", "aiohttp"):
            with self.subTest(missing=missing):
                def blocked(name, *args, **kwargs):
                    if name == missing:
                        raise ImportError("synthetic missing dependency")
                    return real_import(name, *args, **kwargs)
                with patch.object(builtins, "__import__", blocked):
                    self.assertFalse(plugin.check_requirements())
        self.assertTrue(plugin.check_requirements())


class PublicRegistrationTests(unittest.TestCase):
    def test_owner_guide_is_registered_without_a_private_deploy_script(self):
        from hermes_cli.plugins import PluginContext, PluginManager
        from hermes_cli.plugins_manifest import parse_manifest_file
        plugin = load_plugin()
        manifest = parse_manifest_file(ROOT / "plugin.yaml", ROOT, "user", "")
        manager = PluginManager()
        self.addCleanup(manager.unload)
        plugin.register(PluginContext(manifest, manager))
        skill = manager._plugin_skills.get("jibo-platform:jibo-hermes-integration")
        self.assertIsNotNone(skill)
        self.assertEqual(skill["path"], ROOT / "agent-skills/jibo-hermes-integration/SKILL.md")


class PublicInstallTests(unittest.TestCase):
    def test_fresh_source_only_git_install_records_exact_pin_and_imports(self):
        """Exercise Hermes's real installer; no private history, deployment or robot."""
        files = source_files()
        validate_members(files, [f for f in files if (ROOT / f).is_symlink()],
                         integrated=os.getenv("JIBO_RELEASE_INTEGRATED") == "1")
        with tempfile.TemporaryDirectory(prefix="jibo-public-install-") as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            for name in files:
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            env = {"PATH": os.environ["PATH"], "HOME": str(base / "home"),
                   "HERMES_HOME": str(base / "hermes-home"), "TERM": "dumb",
                   "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(base / "tmp")}
            for name in ("home", "hermes-home", "tmp"):
                (base / name).mkdir()
            # An editable public Hermes core is allowed; never borrow plugin/data paths.
            import hermes_cli
            env["PYTHONPATH"] = str(Path(hermes_cli.__file__).resolve().parents[1])

            def run(argv, cwd=base):
                result = subprocess.run(argv, cwd=cwd, env=env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
                self.assertEqual(result.returncode, 0, result.stdout)
                return result.stdout

            run(["git", "init", "--quiet"], cwd=source)
            run(["git", "add", "."], cwd=source)
            run(["git", "-c", "user.name=Public fixture", "-c", "user.email=fixture@example.test",
                 "commit", "--quiet", "-m", "Synthetic public-only fixture"], cwd=source)
            revision = run(["git", "rev-parse", "HEAD"], cwd=source).strip()
            self.assertRegex(revision, r"^[0-9a-f]{40}$")
            cli = [sys.executable, "-m", "hermes_cli.main", "plugins"]
            from tools.plugin_guard import scan_plugin, format_scan_report
            scan = scan_plugin(source, source=source.as_uri())
            self.assertIn(scan.verdict, ("safe", "caution"), format_scan_report(scan))
            self.assertFalse(any(f.severity in {"critical", "high"} for f in scan.findings),
                             format_scan_report(scan))
            # Explicit consent only to this reviewed synthetic fixture's caution findings.
            # Scanning stays on and dangerous verdicts remain blocked by Hermes.
            consent = ["--force"] if scan.verdict == "caution" else []
            installed_output = run(cli + ["install", source.as_uri(), "--ref", revision, "--enable"] + consent)
            installed = base / "hermes-home/plugins/jibo-platform"
            self.assertTrue(installed.is_dir(), installed_output)
            self.assertEqual(run(["git", "rev-parse", "HEAD"], cwd=installed).strip(), revision)
            metadata = json.loads((base / "hermes-home/plugins/.install-metadata.json").read_text())
            self.assertEqual(metadata["jibo-platform"]["revision"], revision)
            self.assertTrue(metadata["jibo-platform"]["pinned"])
            listed = json.loads(run(cli + ["list", "--json", "--no-bundled"]))
            row = next(entry for entry in listed if entry["name"] == "jibo-platform")
            self.assertEqual(row["status"], "enabled")
            self.assertEqual(row["version"], "0.3.1")
            self.assertTrue(row["source"].startswith("git"), row)
            # Exact pin is independently verified in both metadata and Git HEAD above.
            # Some older Hermes builds list only 'git', not a pinned@ badge.
            self.assertFalse((installed / "animdb.json").exists())
            self.assertFalse((installed / "expression_vocabulary.json").exists())
            self.assertEqual(source_files(installed), files)
            for name in files:
                self.assertEqual((installed / name).read_bytes(), (ROOT / name).read_bytes())
            probe = r"""
import importlib, importlib.util, json, pathlib, socket, sys
root = pathlib.Path(sys.argv[1])
def no_network(*args, **kwargs):
    raise AssertionError("import/registration must not contact a robot or provider")
socket.socket.connect = no_network
socket.create_connection = no_network
spec = importlib.util.spec_from_file_location("jibo_source_only", root / "__init__.py", submodule_search_locations=[str(root)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
modules = []
for path in sorted(root.glob("*.py")):
    if path.name != "__init__.py":
        module = importlib.import_module(spec.name + "." + path.stem)
        assert pathlib.Path(module.__file__).resolve().is_relative_to(root)
        modules.append(path.stem)
api = importlib.import_module(spec.name + ".dashboard.plugin_api")
assert pathlib.Path(api.__file__).resolve().is_relative_to(root)
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import parse_manifest_file
manifest = parse_manifest_file(root / "plugin.yaml", root, "user", "")
assert manifest is not None and set(manifest.python_dependencies)
manager = PluginManager()
plugin.register(PluginContext(manifest, manager))
assert plugin.check_requirements()
assert "jibo-platform:jibo-hermes-integration" in manager._plugin_skills
from jibo_source_only.catalog import load_catalog
from jibo_source_only.catalog_tool import jibo_find_expression_available, jibo_play_expression_available
assert load_catalog() == []
assert not jibo_find_expression_available() and not jibo_play_expression_available()
manager.unload()
print(json.dumps({"modules": modules, "dashboard_api": True, "registration": True}))
"""
            imported = json.loads(run([sys.executable, "-c", probe, str(installed)]).splitlines()[-1])
            self.assertIn("native_adapter", imported["modules"])
            self.assertIn("adapter", imported["modules"])
            self.assertTrue(imported["dashboard_api"])
            self.assertTrue(imported["registration"])


if __name__ == "__main__":
    unittest.main()
