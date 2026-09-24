- **Use a protoAgent from Zed's Agent Panel over ACP (ADR 0111, #PR).** New standalone
  `protoagent-acp` package (`integrations/zed-acp/`, run with `uvx`) is a stdio Agent Client
  Protocol server that talks to any running instance over A2A. Zed streams the agent's answer
  and thinking, shows its tool calls as typed cards, follows it into the files it reads and
  searches, turns approval requests into Zed permission prompts, and maps Stop to
  `CancelTask`. It needs no core change and works against local, fleet-member and remote
  instances. Paste-ready Zed `agent_servers` snippet in the package README.
