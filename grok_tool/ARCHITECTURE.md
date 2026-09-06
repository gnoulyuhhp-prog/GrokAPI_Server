# Grok Tool — Modular Architecture (step 2)

All logic lives under **`grokreg/`**. Root `*.py` files are **thin shims** for backward compatibility
(`import anti_flag` still works).

```
grok_tool/
├── main.py                 # thin entry + re-exports
├── config.json / config.example.json
├── *.py                    # shims → grokreg.* (do not add logic here)
├── grokreg/
│   ├── core/               # runtime, config, helpers, paths_cfg, stop_control, style_log
│   ├── mail/               # mail_api, providers, tmail_wibu, temp_mail_router
│   ├── browser/            # chrome, page_flow, anti_flag, chrome_cleanup
│   ├── reg/                # register_one pipeline
│   ├── cli/                # argparse + async main
│   ├── delivery/           # sub2api_client, sub2api_oauth, delivery_retry, sso, gsheets
│   ├── captcha/            # turnstile_solver_client
│   └── tools/              # batch/overnight/continue/probe/ui_menu/reports
├── web_console/            # FastAPI Aurora control plane
├── services/               # Camoufox turnstile solver
├── data/                   # runtime data
├── _root_modules_bak/      # pre-move backup of root modules
└── main.py.monolith.bak    # pre-split main.py
```

## Run

```bash
python main.py 0 --count 1
python -m grokreg.cli.app 0 --count 1
python -m web_console.daemon
python -m grokreg.tools.batch_runner
```

## Adding code

Put new modules under `grokreg/<area>/`.  
Only add a root shim if external scripts still `import old_name`.

## Registration backends

| Backend | Flag | Notes |
|---------|------|--------|
| **browser** | `--backend browser` (default) | pydoll Chrome UI ~2–3 min |
| **protocol** | `--backend protocol` | pure HTTP (competitor path) ~30s; needs Turnstile solver `:5072` |
| **auto** | `--backend auto` | try protocol → fallback browser |

```bash
# Protocol (fast) — solver must be online
python main.py 0 --count 1 --backend protocol

# Browser (stable)
python main.py 0 --count 1 --backend browser
```

Package: `grokreg/protocol/` (backend ported from grok-register-web + worker wired to our mail/Sub2API).
