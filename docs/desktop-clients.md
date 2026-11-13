# Connect Sill to your client

Run [install and first use](onboarding/01-install.md) with the target below.
For another client on an existing install, run the matching `sill connect`
command from the Sill checkout. It merges the Sill entry with the
client's existing settings. `--dry-run` previews whether the registration
can be made. Restart the client and allow the specific Sill calls when asked.

Each local registration points to the installed `sill-mcp` executable and
passes the absolute `backend/.env` path as a separate `--env-file` argument.
Keep the file private. Set the PostgreSQL coordinates or `SILL_DB_DSN` in
that file. The explicit file supplies literal values for the database,
independent of the client's working directory and ambient PostgreSQL
settings. Hook commands use `SILL_INSTANCE_ENV_FILE` to select that same
file. These are separate settings.

If the client has a legacy entry containing only `sill-mcp`, check which
database it used before adopting it. Once confirmed, add `--adopt-legacy`
to the matching registration or [upgrade](../README.md#upgrading) command.
Review custom entries yourself. Default memory tools work with the local
embeddings service. The separate `stage` tool requires an explicit
`SILL_ENABLE_STAGE=1` in the server process environment and a working
optional Chorus installation.

## Claude Desktop

Install with `./install.sh --client claude-desktop` on macOS. For an
existing install:

```bash
sill connect claude-desktop --env-file "$(pwd -P)/backend/.env"
```

1. Quit Claude Desktop completely with **Claude > Quit Claude**, then
   reopen it.
2. In **Settings > Desktop app > Extensions > Local MCP servers**, check
   the Sill connection and its logs. **Edit config** opens
   `~/Library/Application Support/Claude/claude_desktop_config.json`,
   where the entry is `mcpServers.sill`.
3. Open a new chat and look for Sill under **+ > Connectors**. Allow the
   requested tool calls and run the first-memory exercise. If local MCP
   servers are disabled on a managed Mac, check the organization's local
   MCP policy. [Claude Desktop local server guide](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)

## Claude Code

Install with `./install.sh --client claude-code`. For an existing install:

```bash
sill connect claude-code --env-file "$(pwd -P)/backend/.env"
```

Restart Claude Code. `claude mcp list` should show Sill connected. Its
user-scope entry is `mcpServers.sill` in `~/.claude.json`. Accept workspace
trust if required, then allow the Sill tools in the session.

## Codex CLI

Install with `./install.sh --client codex`. For an existing install:

```bash
sill connect codex --env-file "$(pwd -P)/backend/.env"
```

Restart Codex. `codex mcp list` shows the registration, and `/mcp` inside a
new session shows the connected server and tools. Check `[mcp_servers.sill]`
in `${CODEX_HOME:-$HOME/.codex}/config.toml` if the server is missing. Allow
the selected Sill tools when prompted. If you wired optional Codex CLI
hooks, use `/hooks` to review and trust them again after a command changes.
[Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
· [Codex hook review](https://learn.chatgpt.com/docs/hooks)

## Desktop local Codex

Use `./install.sh --client codex` or the `sill connect codex` command
above. The target writes `[mcp_servers.sill]` in
`${CODEX_HOME:-$HOME/.codex}/config.toml` without requiring the Codex CLI.
Run the registration on the same local host and with the Codex home
used by the desktop app. Open **Settings > MCP servers**, check that
Sill is enabled, then use **Restart** or quit and reopen the app. In a
new Codex session running locally on the Mac, check `/mcp` and allow
the requested Sill tool calls. [Desktop MCP settings](https://learn.chatgpt.com/docs/extend/mcp?surface=desktop)

## Client capabilities and verification

| Client | Deliberate MCP memory tools | Automatic recall in prompts | Sill transcript capture | Sill guards | Observed version and mode |
|---|---|---|---|---|---|
| Claude Desktop | Local stdio | No Sill hooks | None from this integration | None from this integration | 5.11492.2 remembered and recalled in two chats at `f3233a5` (reported Sill 0.2.1). Final 0.3 round trip pending. |
| Claude Code | Local stdio | Optional Claude Code hook | Local CLI JSONL for hook processing | Optional Claude Code hooks | 2.1.296 remembered and recalled in separate processes using Sill 0.2.1. Final 0.3 round trip pending. |
| Codex CLI | Local stdio | Optional Codex hook | Local CLI JSONL for hook processing | Optional, reviewed Codex hooks | 0.162.1 recalled the same Sill 0.2.1 UUID from Claude Code. Final 0.3 round trip pending. |
| Desktop local Codex | Local stdio in the app's Codex host | Local hook support in the host. Sill behavior unverified. | Sill capture unverified | Sill enforcement unverified | Sill tool round trip pending. |
| Hosted ChatGPT Chat / Work | Private custom MCP plugin via Secure MCP Tunnel | Explicit tool calls | None from this integration | None from this integration | Tunnel-client 0.0.20 command and profile setup checked locally. Hosted tool round trip pending. |

The CLI and Claude Desktop observations used earlier builds reporting Sill 0.2.1; they do not establish final 0.3 acceptance. The separate CLI
worker callback and transcript checks exercised the launchers and
recorder. See [harness adapters](adapters.md) for the Sill hook tests
and their scope. The first-memory prompts guide deliberate tool use in
any connected client.

## Hosted ChatGPT Chat and Work via a private tunnel

This route is pending an end-to-end Sill round trip from a hosted ChatGPT
conversation. Local Codex and Claude use their own app sign-ins. This
separate hosted setup needs a Platform organization with Secure MCP
Tunnel access and a ChatGPT workspace that permits creating and using a
private custom MCP plugin.

1. In Platform tunnel settings, find or create a private tunnel associated
   with the intended ChatGPT workspace. Creating or editing the tunnel
   requires **Tunnels Read + Manage**. The runtime key and the person
   selecting the tunnel during plugin creation need **Tunnels Read + Use**.
   ChatGPT workspace or developer-mode access is a separate permission.
   Check both if the tunnel is absent in ChatGPT.
   [OpenAI Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
2. Download OpenAI's published `tunnel-client` for your Mac and run
   `tunnel-client help quickstart`. The flags below were checked against
   0.0.20. Supply the restricted runtime key as
   `CONTROL_PLANE_API_KEY` through a protected environment or secret
   store. Keep key literals out of command arguments, shell history,
   configuration files, logs, repositories, and backups.
3. In the shell with the runtime key and `CONTROL_PLANE_TUNNEL_ID` set,
   create a local stdio profile. Replace both absolute paths with those
   from the local Sill registration. The inner quotes keep paths with
   spaces intact as separate arguments.

   ```bash
   umask 077
   tunnel-client init --sample sample_mcp_stdio_local --profile sill-local \
     --tunnel-id "$CONTROL_PLANE_TUNNEL_ID" \
     --control-plane-api-key-ref env:CONTROL_PLANE_API_KEY \
     --mcp-command '"/absolute/path/to/sill-mcp" --env-file "/absolute/path to/sill/backend/.env"'
   tunnel-client doctor --profile sill-local --explain
   ```

4. After the doctor check passes, run the client in the foreground and
   leave it running through discovery and use. The default health
   listener for this profile is loopback on port 8080. Check readiness
   from another terminal.

   ```bash
   tunnel-client run --profile sill-local
   ```

   ```bash
   curl -fsS http://127.0.0.1:8080/readyz
   ```

   Open the local `/ui` on that listener for status and active polling.
   If the port is occupied, choose a free loopback port with
   `--health-listen-addr` when creating the profile and use that port for
   the readiness check. If the MCP child fails, verify both absolute
   paths, the private env file, and the local Sill runtime.
5. In ChatGPT on the web, go to **Plugins > + > Add custom MCP server**.
   Choose **Tunnel** under **Connection**, select your private tunnel,
   confirm the authentication settings for the stdio binding, and
   create and install the plugin in the intended workspace. Check that
   discovery lists and enables both `remember` and `recall`. In a hosted
   chat, select the installed plugin with `@` and run the
   [first-memory exercise](onboarding/01-install.md#remember-and-recall)
   using a fresh hosted chat for recall.
   [OpenAI custom MCP server guide](https://developers.openai.com/api/docs/guides/custom-mcp-server)

The hosted check is complete once the hosted chat calls Sill and the fresh
chat retrieves the same synthetic UUID. Keep the database ports private
and use one foreground tunnel client per stdio tunnel. Secure MCP Tunnel
is for private connections. Public plugin submission has its own HTTPS
endpoint and authentication requirements.
[OpenAI Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
