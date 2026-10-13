"""Execute the shipped dashboard against a synthetic React/SDK harness."""
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const values = [], calls = [];
let cursor = 0, component;
const hooks = {
  useState: initial => {
    const i = cursor++;
    if (!(i in values)) values[i] = initial;
    return [values[i], value => { values[i] = typeof value === 'function' ? value(values[i]) : value; }];
  },
  useEffect: () => {}, useCallback: fn => fn
};
const React = {createElement: (tag, props, ...children) => ({tag, props: props || {}, children})};
const SDK = {React, hooks, fetchJSON: (path, options) => {
  calls.push({path, options});
  return Promise.reject(new Error('Synthetic peer unavailable'));
}};
const window = {__HERMES_PLUGIN_SDK__: SDK, __HERMES_PLUGINS__: {register: (_, fn) => {component = fn;}}};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {window, console, Promise, setInterval, clearInterval});
values[0] = {devices: []}; values[1] = false;
function render() { cursor = 0; return component(); }
function nodes(root) {
  if (!root || typeof root !== 'object') return [];
  return [root].concat((root.children || []).flat(Infinity).flatMap(nodes));
}
function field(name) {return nodes(render()).find(n => n.tag === 'input' && n.props.name === name);}
function enter(name, value) {field(name).props.onChange({target: {name, value}});}
function submit() {nodes(render()).find(n => n.tag === 'form').props.onSubmit({preventDefault() {}});}
"""


def dashboard(script):
    result = subprocess.run(
        ["node", "-e", HARNESS + script, str(ROOT / "dashboard/dist/index.js")],
        text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


def test_dashboard_preserves_malformed_code_instead_of_repairing_it():
    result = dashboard("""
enter('code', 'AB-2K7extra');
console.log(JSON.stringify({value: field('code').props.value}));
""")
    assert result["value"] == "AB-2K7extra"


def test_dashboard_rejects_whitespace_code_without_post():
    result = dashboard("""
enter('host', '127.0.0.1'); enter('code', ' AB2K7 '); submit();
console.log(JSON.stringify({calls, error: values[2]}));
""")
    assert result["calls"] == []
    assert "5-character" in result["error"]


def test_dashboard_clears_pairing_code_after_failed_attempt():
    result = dashboard("""
enter('host', '127.0.0.1'); enter('code', 'AB2K7'); submit();
setImmediate(() => console.log(JSON.stringify({value: field('code').props.value, calls})));
""")
    assert result["value"] == ""
    assert len(result["calls"]) == 1


def test_dashboard_explains_pairing_agent_first_and_distinct_statuses():
    result = dashboard("""
values[0] = {devices: [{id: 'alias', name: 'Synthetic', host: '127.0.0.1', token_configured: true, enabled: true, state: {connected: false}}]};
console.log(JSON.stringify({tree: render()}));
""")
    rendered = json.dumps(result["tree"], ensure_ascii=False)
    for label in ("Agent Mode → Pairing", "Agent-first", "Installed", "Paired", "Host connected", "Mode on", "Tested", "/hermes", "eight-character", "same profile"):
        assert label in rendered
    assert "Hermes Setup" not in rendered
    assert "local-NLU first" not in rendered
    assert "discovery can be added" not in rendered


def test_dashboard_invalid_port_is_not_silently_defaulted():
    result = dashboard("""
enter('host', '127.0.0.1'); enter('code', 'AB2K7'); enter('port', 'invalid'); submit();
console.log(JSON.stringify({calls, error: values[2]}));
""")
    assert result["calls"] == []
    assert "port" in result["error"].lower()


def test_real_hermes_admission_approval_is_profile_scoped(tmp_path, monkeypatch):
    """Real core inbound/authz/pairing; synthetic adapter/socket, no model API."""
    import asyncio
    from gateway.authz_mixin import GatewayAuthorizationMixin
    from gateway.config import GatewayConfig, Platform
    from gateway.pairing import PairingStore
    import gateway.pairing as pairing
    from gateway.platform_registry import PlatformEntry, platform_registry
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.run_inbound import GatewayInboundMixin
    from gateway.session import SessionSource
    from test_turn_spine import _SpineAdapter

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "config.yaml"))
    for key in ("GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS", "JIBO_ALLOW_ALL_USERS", "JIBO_ALLOWED_USERS", "DISCORD_ALLOW_ALL_USERS", "DISCORD_ALLOWED_USERS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(pairing, "get_default_hermes_root", lambda: tmp_path)
    entry = PlatformEntry(name="jibo", label="Synthetic Jibo", adapter_factory=lambda config: None, check_fn=lambda: True)
    monkeypatch.setitem(platform_registry._entries, "jibo", entry)
    platform = Platform("jibo")
    store = PairingStore(profile="robot-test")
    other_store = PairingStore(profile="other-test")
    adapter = _SpineAdapter(capabilities=("display.text", "speech.speak", "turn.append"))

    class CoreIngress(GatewayInboundMixin, GatewayAuthorizationMixin):
        def __init__(self):
            self.config = GatewayConfig()
            self.adapters = {platform: adapter}
            self._profile_adapters = {"robot-test": {platform: adapter}}
            self.pairing_store = store
            self.pairing_stores = {"robot-test": store, "other-test": other_store}
        def _is_user_authorized_for_source(self, source):
            return self._is_user_authorized(source)
        def _scale_to_zero_note_real_inbound(self):
            pass
        def _hm_pre_gateway_dispatch_hook(self, event, source):
            return event

    source = SessionSource(platform=platform, chat_id="jibo:robot", user_id="jibo-local-user", user_name="Jibo user", chat_type="dm", profile="robot-test")
    event = MessageEvent(text="hello", message_type=MessageType.TEXT, source=source)
    core = CoreIngress()
    async def run():
        await adapter._dispatch_utterance("robot", "hello", {}, turn_id="synthetic-auth-turn")
        assert await core._hm_admit_event(event) is None
        displays = adapter.client.methods("display.text")
        assert displays, "Native first-speaker approval needs a legible screen projection"
        screen = displays[0]["text"]
        code = screen.splitlines()[1]
        assert len(code) == 8
        assert "hermes -p robot-test pairing approve jibo " + code in screen
        assert other_store.approve_code("jibo", code) is None
        assert not core._is_user_authorized(source)
        assert store.approve_code("jibo", code)["user_id"] == "jibo-local-user"
        assert PairingStore(profile="robot-test").is_approved("jibo", "jibo-local-user")
        assert await core._hm_admit_event(event) is not None
        unrelated = SessionSource(platform=Platform.DISCORD, chat_id="unrelated", user_id="jibo-local-user", chat_type="dm")
        assert not core._is_user_authorized(unrelated)
        other_profile = SessionSource(platform=platform, chat_id="jibo:robot", user_id="jibo-local-user", chat_type="dm", profile="other-test")
        assert not core._is_user_authorized(other_profile)
    asyncio.run(run())


def test_empty_platform_list_still_exposes_default_plugin_mcp_schemas_and_calls(tmp_path, monkeypatch):
    """Actual resolver, schemas and bridge dispatch; discovery metadata is synthetic."""
    from hermes_cli import tools_config
    import model_tools
    import toolsets
    from tools.registry import registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("XAI_API_KEY", "synthetic-never-used-provider-key")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(tools_config, "_get_plugin_toolset_keys", lambda: {"synthetic_plugin"})
    actions = []
    monkeypatch.setattr(registry, "_tools", dict(registry._tools))
    for toolset, name in (("synthetic_plugin", "synthetic_plugin_action"), ("synthetic_mcp", "mcp__synthetic__action")):
        monkeypatch.setitem(toolsets.TOOLSETS, toolset, {"tools": [name], "description": "Synthetic fixture"})
        def handler(arguments, _name=name, **kwargs):
            actions.append(_name)
            return json.dumps({"fixture_action": _name})
        registry.register(name=name, toolset=toolset, schema={"name": name, "description": "Synthetic fixture", "parameters": {"type": "object", "properties": {}}}, handler=handler)
    config = {"platform_toolsets": {"jibo": []}, "mcp_servers": {"synthetic_mcp": {"enabled": True, "command": "fixture-must-not-start"}}}
    selected = tools_config._get_platform_tools(config, "jibo")
    schemas = model_tools.get_tool_definitions(enabled_toolsets=sorted(selected), quiet_mode=True, skip_tool_search_assembly=True)
    names = {schema["function"]["name"] for schema in schemas}
    assert {"synthetic_plugin_action", "mcp__synthetic__action"} <= names
    for name in ("synthetic_plugin_action", "mcp__synthetic__action"):
        result = model_tools.handle_function_call("tool_call", {"name": name, "arguments": {}}, enabled_toolsets=sorted(selected))
        assert json.loads(result)["fixture_action"] == name
    assert actions == ["synthetic_plugin_action", "mcp__synthetic__action"]

    # Even decline known plugin toolsets + no_mcp must account for provider
    # defaults. Current Hermes's composite resolver still injects x_search.
    restricted = {"platform_toolsets": {"jibo": ["no_mcp"]}, "known_plugin_toolsets": {"jibo": ["synthetic_plugin"]}, "mcp_servers": config["mcp_servers"]}
    restricted_selection = tools_config._get_platform_tools(restricted, "jibo")
    assert "synthetic_plugin" not in restricted_selection
    assert "synthetic_mcp" not in restricted_selection
    assert "x_search" in restricted_selection
    for name in ("synthetic_plugin_action", "mcp__synthetic__action"):
        blocked = model_tools.handle_function_call("tool_call", {"name": name, "arguments": {}}, enabled_toolsets=sorted(restricted_selection))
        assert "error" in json.loads(blocked)
    assert len(actions) == 2
