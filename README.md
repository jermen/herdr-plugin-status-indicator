# Herdr Agent Status Indicator

[DMDOX-317](https://dmdox.atlassian.net/browse/DMDOX-317): two rows for every
agent, using Radar's icon font and state colours. Python 3.11+, Linux/macOS,
Herdr 0.9.1+. No pip/npm dependencies, model calls, agent hooks or transcript reads.

The first row shows the agent logo, lifecycle mark and that pane's title:
an animated spinner for working, red question mark for waiting, green check for
done, and an idle/unknown ring. Questions remain until work resumes; completion
persists through an idle report until the pane is focused. Titles use the pane's
label or stripped terminal title, never the tab label that changes with focus.
An actual title rename is reflected. No pane, tab or workspace is renamed.

The second row consumes the atomic schema-v1 snapshot from
[`jermen.agent-usage`](https://ci.services.dmdox.com/jermen/herdr-plugin-agent-usage):

```text
h 7 % · w 55 % · ctx 14 % · 2 subagents
h -- · w -- · ctx 28 %
```

Quota mode is the default for both local and remote sessions. The same `h / w / ctx`
fields remain visible when provider quota is unavailable, including API-key
sessions; missing quotas show `--` instead of switching to spending. Estimated
spending (`d ~$15.00 / w ~$89.00 / m ~$622.00`) remains an explicit mode.

Quota percentages are **used**, not remaining. `h` denotes a five-hour provider
window, `w` its weekly window. Other provider windows retain their own labels.
Hourly, weekly and context fields are yellow when usage is **greater than 40%**
and red when **greater than 80%**. At exactly 40% the normal color remains;
at exactly 80% the field is yellow. Thresholds use the unrounded percentage.
Missing or expired values stay neutral. Spending and status notes stay neutral.
Spending uses `d` for today in the collector's timezone, `w` for rolling seven
days and `m` for rolling 31 days. The collector does not provide hourly spending.
`~$` means API-equivalent estimated token value, not a subscription bill; `*`
attached to a cost means partial pricing. Billed mode displays only explicitly imported session
charges, labelled `reported`, never organization totals or a fallback estimate.

Missing values are `--`, never zero. Expired quota windows are unavailable.
Quota observations older than `quota_max_age` (15 minutes by default), or missing
their observation timestamp, show a separate red `*` after each available hourly
and weekly value: `h 7 % · * · w 55 % · * · ctx 14 %`. The percentages retain their
normal threshold colors; only the asterisks are always red. Additional provider
windows share one field with one trailing marker. Missing/expired values and
context receive no freshness marker. Fresh quota data clears the markers even
when the percentages stay unchanged. This replaces the longer `quota stale` label.
A snapshot older than two minutes says `usage stale`. Session IDs must match
exactly, so replacing an agent in a pane cannot inherit its predecessor's usage.
Collector failures and catch-up state are visible.

## Install

**The plugin runs on the server; the sidebar layout and fonts belong to the
computer running the Herdr UI.** With Herdr's SSH connection these are different
machines. A successful server reload or metadata check does not prove the client
is rendering the custom layout.

Keep a complete checkout at a stable location. On the **UI computer**:

```sh
./install.sh --dry-run  # preview, no writes
./install.sh            # client mode: back up config, install sidebar and font
```

On the **server running the agents**, from inside its Herdr session:

```sh
./install.sh --server --replace-radar --dry-run
./install.sh --server --replace-radar
python3 indicator.py status
```

`install.sh` requires Bash and Python 3.11+ and works from any current directory.
It selects an available Python 3.11+ interpreter; `PYTHON_BIN` overrides that
choice. It never downloads dependencies. Client mode is the default even if a
Herdr socket is present: it does not call Herdr, link plugins or start a watcher.
`--config /path/to/config.toml` selects a nondefault config. Repeated client
installation preserves unrelated settings and does not duplicate its block.

Server installation sets `mode` to `quota`, including in existing plugin configs,
and restarts the watcher. Run `./install.sh --server` on each machine running
agents to apply the default to existing local and remote sessions. To choose a
different mode, use e.g. `./install.sh --server --display-mode estimated` (or
`auto` / `billed`). Other plugin settings and per-agent `modes` overrides are
preserved. The server dry run previews both sidebar and plugin config changes;
client installation never changes the server's display mode.

To enable percentage colors and red freshness markers after updating, rerun `./install.sh --server` on
the server to restart the plugin, then `./install.sh` on each UI computer and
select **reload config** in Herdr's global menu. The new layout uses separately
styled fields. Updated servers retain the plain `si_detail` field, including the
asterisks, for plain-text layouts. Older layouts with percentage colors must be
updated to display the new marker fields.

Omit `--replace-radar` when Radar is not installed. The server installer stops and
disables Radar before replacing its managed sidebar block, clears only Radar's
view override, and leaves its checkout installed for rollback. The native Herdr
workspace ordering remains in charge; Radar's synthetic grouping/spaces styling
is replaced by Herdr defaults. Unrelated config and plugin metadata are preserved.
Unowned agent sidebar tables are refused rather than silently overwritten.
The server config is reloaded without restarting the server or its terminal
sessions. Then use the Herdr UI's global menu → **reload config** to refresh
the client's presentation. `herdr server reload-config` alone does not do this.

### Herdr UI on another computer (SSH)

On the computer running the UI:

1. Run `./install.sh` from the complete checkout. It installs the sidebar and
   user font with a config backup. Use `--replace-radar` if that config still has
   Radar's managed sidebar. It refuses unowned agent rows instead of deleting
   them. Existing font files are preserved.
2. Run `herdr --help` there if you need to check its local config path. Defaults are
   `~/.config/herdr/config.toml` on Linux/macOS and `%APPDATA%\herdr\config.toml`
   on Windows. Back up that file before editing.
3. For manual installation without Python, copy the complete block from
   [client-sidebar.toml](client-sidebar.toml) into that file. If an agent sidebar
   block already exists, replace that block;
   do not duplicate `[ui.sidebar.agents]`. Remove obsolete
   `[ui.sidebar.agents.rows_by_agent]` overrides, which take precedence over
   the default rows. Preserve unrelated configuration.
   Also install [HerdrAgentIconsMax-Regular.ttf](assets/HerdrAgentIconsMax-Regular.ttf)
   on that same computer. The script does both steps for you.
4. In the Herdr UI, use global menu → **reload config**. Confirm the first line
   shows the agent icon, state and title, and the second shows `h / w / ctx`
   (or spending when explicitly selected). The default `state · workspace · tab` followed by `codex`
   means the client's layout has not been applied.

The client needs no plugin daemon locally. Python is needed only to run the
installer; manual TOML/font installation does not require it. The Bash entrypoint
targets Linux/macOS; Windows clients can use Python directly with
`python install.py --client --apply` (or use Bash if available). Windows font
registration is per user. Linux uses `$XDG_DATA_HOME/fonts`, defaulting to
`~/.local/share/fonts`, and refreshes the font cache when `fc-cache` is available.
The installer prints Ghostty/Kitty fallback settings if your terminal needs
explicit codepoint mapping; it does not modify terminal configs.
The checked-in TOML is also the installer's source, so the two layouts stay equal.
See the [Herdr 0.9.1 configuration documentation](https://github.com/herdrdev/herdr/blob/v0.9.1/docs/next/website/src/content/docs/configuration.mdx#reload-config)
for the client/server split. Metadata validation and a healthy watcher establish
only the server side; visual confirmation must happen in the actual UI.

The font is copied to the user's font directory only if it is absent. On this
computer Radar's terminal font mapping can be reused if already present. For a new terminal,
configure fallback ranges `U+E1A0-U+E1B6,U+E1C0-U+E1C5` to
`Herdr Agent Icons Max`; terminals may need a new window to discover the font.
Set `"icons": "text"` for a portable agent-name/Unicode fallback.

Config: `~/.config/herdr/plugins/config/jermen.status-indicator/config.json`.
The server installer creates it when absent and applies the selected `mode` on
every install; see `config.example.json`.

- `mode`: `quota` (default), `auto` (quota when present, otherwise estimated spending),
  `estimated`, or `billed`.
- `modes`: per-agent overrides, e.g. `{"kimi":"estimated","claude":"quota"}`.
- `usage_snapshot`: optional exact snapshot path for a custom collector setup.
  By default the collector's configured `state_dir` and current endpoint hash
  resolve the file. Context is current-session only.
- `snapshot_max_age`, `quota_max_age`: freshness thresholds in seconds.
- `state_dir`: optional private daemon-state root; each endpoint gets its own
  subdirectory. Standard XDG paths and `HERDR_CONFIG_PATH` are respected.

In a spending mode, when collection fails before spending is available, the second line shows the
reason (for example, `transcript missing`) instead of three empty cost columns.
Available context, quota and previously collected amounts remain visible. To
read the full diagnostic independently of sidebar width, run in a Herdr pane:

```sh
herdr api snapshot | jq -r '.result.snapshot.panes[] | [.pane_id, .tokens.si_detail] | @tsv'
```

After changing plugin settings, invoke `stop` then `start`. One watcher per
endpoint is enforced by a file lock. The watcher exits when its socket is removed
or replaced, and startup/detection hooks start it. Metadata writes are limited to
the plugin's `si_*` tokens; only changed values are published, in batches within
Herdr's 16-token request limit. Unchanged frames are not rewritten.
Collector snapshots are re-read only when their file changes.

## Subagents

Herdr protocol 22 and the usage schema do not expose native internal subagent
counts. No count is fabricated from split panes or old log files. An integration
may publish an integer pane token `subagents_running`; the second line renders
that count, including an explicit zero. The reporting integration must clear or
expire its token when the count is no longer valid. Without it the field is
omitted. Native Claude/Codex subagent discovery remains unsupported.

## Verify and roll back

```sh
python3 -B -m unittest discover -s tests -v
python3 indicator.py preview               # read-only live rows as JSON
python3 indicator.py status                # recent watcher heartbeat
herdr plugin list
herdr plugin action invoke start --plugin jermen.status-indicator
./install.sh --rollback /path/to/client-backup
./install.sh --server --rollback /path/to/server-backup
```

Backups live under `$XDG_STATE_HOME/herdr-status-indicator/backups` (normally
`~/.local/state`). Rollback stops this watcher, clears its metadata, disables it,
restores the saved Herdr and plugin configs, and re-enables/restarts Radar if previously enabled.
It refuses to overwrite either config changed since installation; merge the saved
`config.toml` or `plugin-config.json` manually in that case. A newly created plugin
config is removed on rollback. Older backups without a plugin config retain it.
Font and backup files remain.
Reload the UI config after rollback too. A separate SSH client's config is not
part of the server backup; restore its agent block from its own backup.
Client backups are under `herdr-status-indicator/client-backups` in the same
state root. Client rollback restores only its saved config, refuses subsequent
user edits, and removes the config file only if that install originally created
it. Fonts and backups remain. Client and server backups cannot be mixed.
Runtime errors go to the endpoint's `watch.log`; the `start` action recovers a
stopped watcher. Stopping alone freezes its last display; `clear` removes it.

The tests cover session isolation, focus-stable titles, lifecycle transitions,
quota expiry/freshness, honest spending labels, metadata ownership, a real Unix
socket round trip, and safe/idempotent TOML replacement. Assets and attribution
are documented in `THIRD_PARTY_NOTICES.md`.
