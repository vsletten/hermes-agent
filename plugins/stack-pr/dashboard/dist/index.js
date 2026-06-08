/**
 * Stack PR dashboard plugin.
 *
 * Plain IIFE, no build step. Uses the Hermes dashboard plugin SDK for React,
 * UI primitives, and authenticated fetchJSON calls.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const hooks = SDK.hooks;
  const C = SDK.components || {};
  const Button = C.Button || "button";
  const Input = C.Input || "input";
  const Label = C.Label || "label";
  const Badge = C.Badge || "span";
  const Separator = C.Separator || "hr";
  const useState = hooks.useState;
  const useEffect = hooks.useEffect;
  const useCallback = hooks.useCallback;

  const STATUS_ENDPOINT = "/api/plugins/stack-pr/status";
  const VIEW_ENDPOINT = "/api/plugins/stack-pr/view";
  const SUBMIT_ENDPOINT = "/api/plugins/stack-pr/submit";
  const LAND_ENDPOINT = "/api/plugins/stack-pr/land";
  const ABANDON_ENDPOINT = "/api/plugins/stack-pr/abandon";
  const REPO_STORAGE_KEY = "hermes.stack-pr.repoPath";

  function readStoredRepoPath() {
    try {
      const stored = window.localStorage && window.localStorage.getItem(REPO_STORAGE_KEY);
      return stored || "";
    } catch (_err) {
      return "";
    }
  }

  function writeStoredRepoPath(repoPath) {
    try {
      if (!window.localStorage) return;
      if (repoPath) {
        window.localStorage.setItem(REPO_STORAGE_KEY, repoPath);
      } else {
        window.localStorage.removeItem(REPO_STORAGE_KEY);
      }
    } catch (_err) {
      // Local storage availability should never block the dashboard.
    }
  }

  function parseApiErrorMessage(err) {
    const raw = err && err.message ? String(err.message) : String(err || "");
    const match = raw.match(/^(\d{3}):\s*(.*)$/s);
    const body = match ? match[2] : raw;
    try {
      const parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === "string") return parsed.detail;
      if (parsed && parsed.detail && typeof parsed.detail.message === "string") {
        return parsed.detail.message;
      }
    } catch (_err) {
      // Not JSON; fall through to the raw message.
    }
    return body || raw || "Request failed";
  }

  function statusUrl(repoPath) {
    const trimmed = String(repoPath || "").trim();
    if (!trimmed) return STATUS_ENDPOINT;
    return STATUS_ENDPOINT + "?repo_path=" + encodeURIComponent(trimmed);
  }

  function postJSON(endpoint, body) {
    return SDK.fetchJSON(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  function badge(text, tone) {
    return h(Badge, { className: "stack-pr-badge stack-pr-badge--" + tone }, text);
  }

  function FieldCheckbox(props) {
    const input = h("input", {
      id: props.id,
      type: "checkbox",
      checked: !!props.checked,
      disabled: !!props.disabled,
      onChange: function (event) {
        props.onChange(!!event.target.checked);
      },
    });
    return h("label", {
      className: "stack-pr-check",
      htmlFor: props.id,
    }, input, h("span", null, props.children));
  }

  function ToolStatus(props) {
    const tool = props.tool || {};
    return h("div", { className: "stack-pr-tool" },
      h("div", { className: "stack-pr-tool-name" }, props.name),
      tool.available ? badge("available", "ok") : badge("missing", "warn"),
      h("div", { className: "stack-pr-tool-path" }, tool.path || "not found")
    );
  }

  function StatusPanel(props) {
    const status = props.status;
    if (!status) {
      return h("div", { className: "stack-pr-panel stack-pr-panel--muted" },
        h("div", { className: "stack-pr-panel-title" }, "Status"),
        h("div", { className: "stack-pr-muted" }, "Waiting for status.")
      );
    }

    const tools = status.tools || {};
    const repo = status.repo;
    return h("div", { className: "stack-pr-panel" },
      h("div", { className: "stack-pr-panel-title" }, "Status"),
      h("div", { className: "stack-pr-tools" },
        h(ToolStatus, { name: "git", tool: tools.git }),
        h(ToolStatus, { name: "gh", tool: tools.gh }),
        h(ToolStatus, { name: "stack-pr", tool: tools["stack-pr"] })
      ),
      h(Separator, { className: "stack-pr-separator" }),
      repo
        ? h("div", { className: "stack-pr-repo-status" },
            h("div", { className: "stack-pr-row" },
              h("span", { className: "stack-pr-label" }, "Repo"),
              repo.valid ? badge("valid", "ok") : badge("invalid", "warn")
            ),
            h("div", { className: "stack-pr-path" }, repo.path || ""),
            repo.error ? h("div", { className: "stack-pr-error-text" }, repo.error) : null
          )
        : h("div", { className: "stack-pr-muted" }, "Enter an absolute Git worktree path.")
    );
  }

  function OutputBlock(props) {
    if (!props.value) return null;
    return h("div", { className: "stack-pr-output-block" },
      h("div", { className: "stack-pr-output-label" }, props.label),
      h("pre", null, props.value)
    );
  }

  function ResultPanel(props) {
    const result = props.result;
    if (!result && !props.error) {
      return h("div", { className: "stack-pr-panel stack-pr-panel--muted" },
        h("div", { className: "stack-pr-panel-title" }, "Result"),
        h("div", { className: "stack-pr-muted" }, "No command result yet.")
      );
    }

    if (props.error) {
      return h("div", { className: "stack-pr-panel stack-pr-panel--error" },
        h("div", { className: "stack-pr-panel-title" }, "Actionable error"),
        h("div", { className: "stack-pr-error-text" }, props.error)
      );
    }

    return h("div", { className: "stack-pr-panel" },
      h("div", { className: "stack-pr-panel-head" },
        h("div", { className: "stack-pr-panel-title" }, "Result"),
        result.ok ? badge("ok", "ok") : badge("failed", "warn")
      ),
      h("div", { className: "stack-pr-result-grid" },
        h("div", null, h("span", { className: "stack-pr-label" }, "Command"), h("code", null, (result.argv || []).join(" "))),
        h("div", null, h("span", { className: "stack-pr-label" }, "Exit code"), h("code", null, result.exit_code === null || result.exit_code === undefined ? "timeout" : String(result.exit_code)))
      ),
      result.parsed_text ? h("div", { className: "stack-pr-parsed" }, result.parsed_text) : null,
      h(OutputBlock, { label: "stdout", value: result.stdout }),
      h(OutputBlock, { label: "stderr", value: result.stderr })
    );
  }

  function StackPrPage() {
    const initialRepoPath = readStoredRepoPath();
    const state = useState(initialRepoPath);
    const repoPath = state[0];
    const setRepoPath = state[1];
    const statusState = useState(null);
    const status = statusState[0];
    const setStatus = statusState[1];
    const resultState = useState(null);
    const result = resultState[0];
    const setResult = resultState[1];
    const errorState = useState("");
    const error = errorState[0];
    const setError = errorState[1];
    const busyState = useState("");
    const busy = busyState[0];
    const setBusy = busyState[1];
    const submitConfirmState = useState(false);
    const submitConfirm = submitConfirmState[0];
    const setSubmitConfirm = submitConfirmState[1];
    const landConfirmState = useState(false);
    const landConfirm = landConfirmState[0];
    const setLandConfirm = landConfirmState[1];
    const abandonTextState = useState("");
    const abandonText = abandonTextState[0];
    const setAbandonText = abandonTextState[1];

    const trimmedRepoPath = String(repoPath || "").trim();
    const commandBusy = !!busy;
    const repoMissing = !trimmedRepoPath;

    const refreshStatus = useCallback(function () {
      setBusy("status");
      setError("");
      return SDK.fetchJSON(statusUrl(trimmedRepoPath))
        .then(function (data) {
          setStatus(data);
        })
        .catch(function (err) {
          setError(parseApiErrorMessage(err));
        })
        .then(function () {
          setBusy("");
        });
    }, [trimmedRepoPath]);

    useEffect(function () {
      refreshStatus();
    }, [refreshStatus]);

    function onRepoPathChange(event) {
      const next = event.target.value;
      setRepoPath(next);
      writeStoredRepoPath(next);
    }

    function runView() {
      if (repoMissing) {
        setError("Enter an absolute Git worktree path before running stack-pr view.");
        return;
      }
      setBusy("view");
      setError("");
      setResult(null);
      postJSON(VIEW_ENDPOINT, { repo_path: trimmedRepoPath })
        .then(function (data) {
          setResult(data);
          return refreshStatus();
        })
        .catch(function (err) {
          setError(parseApiErrorMessage(err));
        })
        .then(function () {
          setBusy("");
        });
    }

    function runSubmit() {
      if (repoMissing) {
        setError("Enter an absolute Git worktree path before running stack-pr submit.");
        return;
      }
      if (!submitConfirm) {
        setError("Confirm stack-pr submit before running it.");
        return;
      }
      setBusy("submit");
      setError("");
      setResult(null);
      postJSON(SUBMIT_ENDPOINT, { repo_path: trimmedRepoPath, confirm: true })
        .then(function (data) {
          setSubmitConfirm(false);
          setResult(data);
          return refreshStatus();
        })
        .catch(function (err) {
          setError(parseApiErrorMessage(err));
        })
        .then(function () {
          setBusy("");
        });
    }

    function runLand() {
      if (repoMissing) {
        setError("Enter an absolute Git worktree path before running stack-pr land.");
        return;
      }
      if (!landConfirm) {
        setError("Confirm stack-pr land before running it.");
        return;
      }
      setBusy("land");
      setError("");
      setResult(null);
      postJSON(LAND_ENDPOINT, { repo_path: trimmedRepoPath, confirm: true })
        .then(function (data) {
          setLandConfirm(false);
          setResult(data);
          return refreshStatus();
        })
        .catch(function (err) {
          setError(parseApiErrorMessage(err));
        })
        .then(function () {
          setBusy("");
        });
    }

    function runAbandon() {
      if (repoMissing) {
        setError("Enter an absolute Git worktree path before running stack-pr abandon.");
        return;
      }
      if (abandonText !== "abandon") {
        setError("Type abandon to confirm stack-pr abandon.");
        return;
      }
      setBusy("abandon");
      setError("");
      setResult(null);
      postJSON(ABANDON_ENDPOINT, { repo_path: trimmedRepoPath, confirm_text: "abandon" })
        .then(function (data) {
          setAbandonText("");
          setResult(data);
          return refreshStatus();
        })
        .catch(function (err) {
          setError(parseApiErrorMessage(err));
        })
        .then(function () {
          setBusy("");
        });
    }

    return h("div", { className: "stack-pr" },
      h("div", { className: "stack-pr-header" },
        h("div", null,
          h("h1", null, "Stack PR"),
          h("div", { className: "stack-pr-subtitle" }, "Local stack-pr controls")
        ),
        h(Button, {
          onClick: refreshStatus,
          disabled: commandBusy,
        }, busy === "status" ? "Checking..." : "Refresh status")
      ),
      h("div", { className: "stack-pr-grid" },
        h("div", { className: "stack-pr-main" },
          h("div", { className: "stack-pr-panel" },
            h("div", { className: "stack-pr-field" },
              h(Label, { htmlFor: "stack-pr-repo-path" }, "Repo path"),
              h(Input, {
                id: "stack-pr-repo-path",
                value: repoPath,
                onChange: onRepoPathChange,
                placeholder: "/absolute/path/to/git/worktree",
                spellCheck: false,
                autoComplete: "off",
              })
            ),
            h("div", { className: "stack-pr-actions" },
              h(Button, {
                onClick: runView,
                disabled: commandBusy || repoMissing,
              }, busy === "view" ? "Running view..." : "View stack")
            )
          ),
          h("div", { className: "stack-pr-panel" },
            h("div", { className: "stack-pr-panel-title" }, "Mutating actions"),
            h("div", { className: "stack-pr-action-row" },
              h("div", null,
                h("div", { className: "stack-pr-action-name" }, "Submit"),
                h(FieldCheckbox, {
                  id: "stack-pr-submit-confirm",
                  checked: submitConfirm,
                  disabled: commandBusy,
                  onChange: setSubmitConfirm,
                }, "Confirm stack-pr submit")
              ),
              h(Button, {
                onClick: runSubmit,
                disabled: commandBusy || repoMissing || !submitConfirm,
              }, busy === "submit" ? "Submitting..." : "Submit")
            ),
            h("div", { className: "stack-pr-action-row" },
              h("div", null,
                h("div", { className: "stack-pr-action-name" }, "Land"),
                h(FieldCheckbox, {
                  id: "stack-pr-land-confirm",
                  checked: landConfirm,
                  disabled: commandBusy,
                  onChange: setLandConfirm,
                }, "Confirm stack-pr land")
              ),
              h(Button, {
                onClick: runLand,
                disabled: commandBusy || repoMissing || !landConfirm,
              }, busy === "land" ? "Landing..." : "Land")
            ),
            h("div", { className: "stack-pr-action-row stack-pr-action-row--danger" },
              h("div", { className: "stack-pr-abandon-field" },
                h("div", { className: "stack-pr-action-name" }, "Abandon"),
                h(Label, { htmlFor: "stack-pr-abandon-confirm" }, "Type abandon to confirm"),
                h(Input, {
                  id: "stack-pr-abandon-confirm",
                  value: abandonText,
                  onChange: function (event) { setAbandonText(event.target.value); },
                  placeholder: "abandon",
                  autoComplete: "off",
                  spellCheck: false,
                })
              ),
              h(Button, {
                onClick: runAbandon,
                disabled: commandBusy || repoMissing || abandonText !== "abandon",
              }, busy === "abandon" ? "Abandoning..." : "Abandon")
            )
          ),
          h(ResultPanel, { result: result, error: error })
        ),
        h("div", { className: "stack-pr-side" },
          h(StatusPanel, { status: status })
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register("stack-pr", StackPrPage);
})();
