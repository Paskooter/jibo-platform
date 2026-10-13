(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !SDK.React || !SDK.hooks) {
    if (window.console) {
      console.error("[jibo-platform] Hermes dashboard SDK unavailable");
    }
    return;
  }

  var React = SDK.React;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useCallback = SDK.hooks.useCallback;
  var h = React.createElement;
  var BASE = "/api/plugins/jibo-platform";

  function api(path, options) {
    if (typeof SDK.fetchJSON !== "function") {
      return Promise.reject(new Error("Hermes plugin SDK unavailable"));
    }
    return SDK.fetchJSON(BASE + path, options);
  }

  function text(value, fallback) {
    return value === undefined || value === null || value === "" ? (fallback || "") : String(value);
  }

  function modelKey(model, provider) {
    return String(model || "") + "|" + String(provider || "");
  }

  function deviceModelKey(device) {
    return modelKey(device && device.model, device && device.model_provider);
  }

  // The choices are the models Hermes has actually run lately plus whatever
  // this robot is set to, never an exhaustive catalogue: this select and the
  // robot's own screen are both too small to read one.
  function modelChoices(models, device) {
    var recent = (models && Array.isArray(models.recent)) ? models.recent : [];
    var seen = {};
    var out = [];
    recent.forEach(function (item) {
      var key = modelKey(item.model, item.provider);
      if (seen[key]) { return; }
      seen[key] = true;
      out.push({ key: key, model: item.model, provider: item.provider });
    });
    var current = deviceModelKey(device);
    if (device && device.model && !seen[current]) {
      out.unshift({ key: current, model: device.model, provider: device.model_provider });
    }
    return out;
  }

  function connected(device) {
    return !!(device && device.state && device.state.connected);
  }

  function statusLabel(device) {
    if (connected(device)) { return "Connected"; }
    if (device && device.restart_required) { return "Restart required"; }
    if (device && device.awaiting_new_session) { return "Awaiting new session"; }
    return device && device.enabled === false ? "Disabled" : "Offline";
  }

  function statusClass(device) {
    return connected(device) ? "jibo-status jibo-status-connected" : "jibo-status";
  }

  function JiboDashboard() {
    var overviewState = useState(null);
    var overview = overviewState[0];
    var setOverview = overviewState[1];
    var loadingState = useState(true);
    var loading = loadingState[0];
    var setLoading = loadingState[1];
    var errorState = useState("");
    var error = errorState[0];
    var setError = errorState[1];
    var selectedState = useState("");
    var selectedId = selectedState[0];
    var setSelectedId = selectedState[1];
    var savingState = useState(false);
    var saving = savingState[0];
    var setSaving = savingState[1];
    var messageState = useState("");
    var message = messageState[0];
    var setMessage = messageState[1];
    var formState = useState({ id: "", name: "", host: "", port: "8890", code: "" });
    var form = formState[0];
    var setForm = formState[1];
    var modelsState = useState(null);
    var models = modelsState[0];
    var setModels = modelsState[1];

    // The default and the recent list are the same for every robot; only the
    // current choice is per device, and that arrives with the overview.
    var loadModels = useCallback(function () {
      return api("/models")
        .then(function (value) { setModels(value || null); return value; })
        .catch(function () { return null; });
    }, []);

    function chooseModel(deviceId, key) {
      var parts = String(key || "").split("|");
      setSaving(true);
      setError("");
      setMessage("");
      api("/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: deviceId, model: parts[0] || "", provider: parts[1] || "" })
      }).then(function (result) {
        setMessage(parts[0]
          ? "Now answering on " + parts[0] + "."
          : "Back to the Hermes default model.");
        if (result && Array.isArray(result.recent)) { setModels(result); }
        return load();
      }).catch(function (reason) {
        setError(reason && reason.message ? reason.message : String(reason));
      }).finally(function () {
        setSaving(false);
      });
    }

    var load = useCallback(function () {
      setError("");
      return api("/overview")
        .then(function (value) {
          var next = value || { devices: [] };
          setOverview(next);
          if (!selectedId && next.devices && next.devices.length) {
            setSelectedId(next.devices[0].id);
          }
          return next;
        })
        .catch(function (reason) {
          setError(reason && reason.message ? reason.message : String(reason));
          return null;
        })
        .finally(function () {
          setLoading(false);
        });
    }, [selectedId]);

    useEffect(function () {
      load();
      loadModels();
      var timer = setInterval(load, 10000);
      return function () { clearInterval(timer); };
    }, [load, loadModels]);

    function updateField(event) {
      var key = event.target.name;
      var value = event.target.value;
      setForm(function (previous) {
        var next = {};
        Object.keys(previous).forEach(function (item) { next[item] = previous[item]; });
        next[key] = value;
        return next;
      });
    }

    function pairDevice(event) {
      event.preventDefault();
      if (!form.host.trim()) {
        setError("Enter a Jibo IP address or hostname.");
        return;
      }
      if (!/^[A-HJ-NP-Z2-9]{5}$/i.test(form.code)) {
        setError("Enter the 5-character pairing code shown on the Jibo.");
        return;
      }
      if (!/^[0-9]+$/.test(form.port) || Number(form.port) < 1 || Number(form.port) > 65535) {
        setError("Enter a port from 1 to 65535 (normally 8890).");
        return;
      }
      var existing = overview && Array.isArray(overview.devices) ? overview.devices : [];
      var nextId = form.id || ("jibo-" + (existing.length + 1));
      if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(nextId)) {
        setError("Use a local alias with letters, numbers, '.', '_' or '-'; no spaces.");
        return;
      }
      if (existing.some(function (item) { return item.id === nextId; })) {
        setError("That device id is already configured.");
        return;
      }
      setSaving(true);
      setError("");
      setMessage("");
      api("/pair", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: nextId,
          name: form.name.trim() || nextId,
          host: form.host.trim(),
          port: Number(form.port),
          code: form.code.toUpperCase()
        })
      }).then(function (result) {
        setOverview(result && result.overview ? result.overview : overview);
        if (result && result.device && result.device.id) {
          setSelectedId(result.device.id);
        }
        setMessage("Paired: token saved privately. Restart the selected Hermes gateway to connect, using the same profile as this dashboard. Restarting can interrupt its other platforms.");
        setForm({ id: "", name: "", host: "", port: "8890", code: "" });
      }).catch(function (reason) {
        setError(reason && reason.message ? reason.message : String(reason));
      }).finally(function () {
        setSaving(false);
        setForm(function (previous) {
          return Object.assign({}, previous, { code: "" });
        });
      });
    }

    function removeDevice(id) {
      var existing = overview && Array.isArray(overview.devices) ? overview.devices : [];
      saveDevices(existing.filter(function (item) { return item.id !== id; }));
      if (selectedId === id) {
        setSelectedId("");
      }
    }

    function saveDevices(devices) {
      setSaving(true);
      setError("");
      setMessage("");
      api("/devices", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ devices: devices })
      }).then(function (result) {
        setOverview(result && result.overview ? result.overview : overview);
        setMessage(result && result.restart_required
          ? "Saved. Restart the selected Hermes gateway, using the same profile, to apply the device registry. Remove only changes this computer’s config; it does not revoke Jibo’s stored token."
          : "Saved.");
        setForm({ id: "", name: "", host: "", port: "8890", code: "" });
      }).catch(function (reason) {
        setError(reason && reason.message ? reason.message : String(reason));
      }).finally(function () {
        setSaving(false);
      });
    }

    var devices = overview && Array.isArray(overview.devices) ? overview.devices : [];
    var selected = devices.filter(function (item) { return item.id === selectedId; })[0] || devices[0] || null;
    var logs = selected && Array.isArray(selected.chat_log) ? selected.chat_log.slice().reverse() : [];

    if (loading && !overview) {
      return h("div", { className: "jibo-dashboard" }, h("p", null, "Loading Jibo bridge…"));
    }

    return h("div", { className: "jibo-dashboard" },
      h("div", { className: "jibo-header" },
        h("div", null,
          h("h1", null, "Jibo · Native Hermes"),
          h("p", { className: "jibo-muted" }, "Agent-first while Agent Mode is on and its host is connected. Off or host-off returns to Classic; explicit legacy modes remain optional.")
        ),
        h("button", { type: "button", className: "jibo-button", onClick: load }, "Refresh")
      ),
      error ? h("div", { className: "jibo-error", role: "alert" }, error) : null,
      message ? h("div", { className: "jibo-message", role: "status" }, message) : null,
      h("div", { className: "jibo-grid" },
        h("section", { className: "jibo-card" },
          h("h2", null, "Configured Jibos"),
          h("p", { className: "jibo-muted" }, "Pair with the five-character code from Agent Mode → Pairing. Your local alias is separate from Jibo’s advertised robot identity. Hermes stores the token privately."),
          devices.length ? h("div", { className: "jibo-device-list" },
            devices.map(function (device) {
              return h("div", {
                key: device.id,
                className: "jibo-device-row" + (selected && selected.id === device.id ? " jibo-device-selected" : ""),
                onClick: function () { setSelectedId(device.id); }
              },
                h("div", null,
                  h("strong", null, text(device.name, device.id)),
                  h("div", { className: "jibo-muted jibo-mono" }, device.host + ":" + text(device.port, "8890"))
                ),
                h("div", { className: "jibo-device-actions" },
                  h("select", {
                    className: "jibo-model-select",
                    title: "Which model answers on this Jibo",
                    value: deviceModelKey(device),
                    disabled: saving,
                    onClick: function (event) { event.stopPropagation(); },
                    onChange: function (event) {
                      event.stopPropagation();
                      chooseModel(device.id, event.target.value);
                    }
                  },
                    h("option", { value: "|" },
                      "Hermes default" + (models && models.default && models.default.model
                        ? " (" + models.default.model + ")"
                        : "")),
                    modelChoices(models, device).map(function (choice) {
                      return h("option", { key: choice.key, value: choice.key }, choice.model);
                    })
                  ),
                  h("span", { className: statusClass(device) }, statusLabel(device)),
                  h("button", {
                    type: "button",
                    className: "jibo-link-button",
                    onClick: function (event) { event.stopPropagation(); removeDevice(device.id); },
                    disabled: saving
                  }, "Remove")
                )
              );
            })
          ) : h("div", { className: "jibo-empty" }, "No Jibos configured yet."),
          h("form", { className: "jibo-form", onSubmit: pairDevice },
            h("h3", null, "Pair a Jibo"),
            h("label", null, "Local device alias", h("input", { name: "id", value: form.id, onChange: updateField, placeholder: "jibo-living-room" })),
            h("label", null, "Display name", h("input", { name: "name", value: form.name, onChange: updateField, placeholder: "Living room Jibo" })),
            h("label", null, "IP or hostname *", h("input", { name: "host", value: form.host, onChange: updateField, placeholder: "192.168.1.42", required: true })),
            h("div", { className: "jibo-form-inline" },
              h("label", null, "Port", h("input", { name: "port", value: form.port, onChange: updateField, inputMode: "numeric" })),
              h("label", null, "Pairing code *", h("input", { name: "code", value: form.code, onChange: updateField, inputMode: "text", autoCapitalize: "characters", autoCorrect: "off", spellCheck: false, placeholder: "AB2K7", required: true }))
            ),
            h("button", { type: "submit", className: "jibo-button jibo-button-primary", disabled: saving }, saving ? "Pairing…" : "Pair Jibo")
          ),
          h("p", { className: "jibo-note" }, "Home-network connection: host/IP, port 8890, path /hermes, ws:// without TLS. Use a compatible Agent Mode app with a network listener; older loopback-only app versions cannot connect through a robot LAN address. Check the app’s release notes. Trusted home network only: traffic is not encrypted; never expose this port to the Internet."),
          h("p", { className: "jibo-note" }, "Gateway commands use the same profile as this dashboard: hermes gateway restart, or hermes -p <profile> gateway restart. Keep the computer and gateway awake."),
          h("p", { className: "jibo-note" }, "Hermes may ask for a separate eight-character authorization code on the first spoken request. Approve it on the computer with hermes pairing approve jibo <code>, or hermes -p <profile> pairing approve jibo <code>. This approves the shared household identity, not an identified voice."),
          h("p", { className: "jibo-note" }, "Review Jibo’s tools in Hermes before enabling Agent Mode. An empty list alone can still inherit plugin/MCP tools; no computer-tool isolation is claimed here. Nearby voices can invoke enabled tools. Camera access is off for this Agent Mode app."),
          h("p", { className: "jibo-note" }, "On Jibo, open Agent Mode → Pairing. The five-character code expires after 15 minutes and works once. Enter it exactly; spaces or punctuation are not removed. Pairing does not enable Agent Mode.")
        ),
        h("section", { className: "jibo-card" },
          h("h2", null, selected ? text(selected.name, selected.id) : "Jibo activity"),
          selected ? h("div", { className: "jibo-details" },
            h("div", null, h("span", { className: statusClass(selected) }, statusLabel(selected)), " ", h("span", { className: "jibo-muted" }, selected.state && selected.state.last_error ? selected.state.last_error : "")),
            h("div", { className: "jibo-muted" }, "Capabilities: " + ((selected.capabilities || []).length ? selected.capabilities.join(", ") : "not connected"))
          ) : h("p", { className: "jibo-muted" }, "Select a device to inspect its session."),
          selected ? h("dl", { className: "jibo-setup-states" },
            h("dt", null, "Installed"), h("dd", null, "Check the compatible Agent Mode app on Jibo"),
            h("dt", null, "Paired"), h("dd", null, selected.token_configured ? "Token saved on this computer" : "No saved token"),
            h("dt", null, "Host connected"), h("dd", null, statusLabel(selected)),
            h("dt", null, "Mode on"), h("dd", null, "Check Enable on Jibo; not verified here"),
            h("dt", null, "Tested"), h("dd", null, "Not verified here. Try a spoken reply, Stop and Classic."),
            h("dt", null, "Robot identity"), h("dd", { className: "jibo-mono" }, selected.robot_device_id || "Legacy token: identity not pinned; alias and token kept unchanged")
          ) : null,
          h("h3", { className: "jibo-log-title" }, "Hermes chat log"),
          logs.length ? h("div", { className: "jibo-log" },
            logs.map(function (entry, index) {
              return h("div", { className: "jibo-log-entry", key: String(entry.at || "") + "-" + index },
                h("div", { className: "jibo-log-meta" }, text(entry.direction, "event") + " · " + text(entry.method, "message") + (entry.route ? " · " + entry.route : "")),
                h("div", { className: "jibo-log-text" }, entry.text !== undefined ? entry.text : text(entry.detail))
              );
            })
          ) : h("div", { className: "jibo-empty" }, "No transcript or assistant messages recorded yet.")
        )
      )
    );
  }

  var hub = window.__HERMES_PLUGINS__;
  if (hub && typeof hub.register === "function") {
    hub.register("jibo-platform", JiboDashboard);
  } else if (window.console) {
    console.error("[jibo-platform] dashboard registration unavailable");
  }
}());
