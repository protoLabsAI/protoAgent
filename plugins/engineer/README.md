# Engineer — navigator skills

The skill pack behind the **Engineer** archetype
([engineer-archetype](https://github.com/protoLabsAI/engineer-archetype)): a hands-on
pair-programming *navigator*. The operator drives and authors every change; the agent
orients, reproduces, points at evidence, and reviews. Prompt-only: no tools, routes,
surfaces, or config of its own.

| Skill | What it does |
|-------|--------------|
| `repo-onboard` | Clone or register a repo, prove the toolchain (mise-aware), take a baseline, write a ≤25-line repo card (saved to memory), then a guided tour of the core flow, **one stop per turn**. |
| `debug-loop` | Seven guided steps, **one checkpoint per turn**: orient → reproduce → narrow → *their* hypothesis → locate (the operator types the fix) → review + verify → the operator commits. Ends with an appendix of why LLM/agent apps "stop without answering". |

Both are agent-retrievable (not slash-only): the Engineer persona reaches for them on
its own. They point at code with `show_code` (the code-pane toolset,
`filesystem.code_pane`) and put the operator's cursor on a line with `open_in_editor`
(`filesystem.editor_command`); each degrades to `path:line` when its toolset is off.

## Enabling

**Off by default.** These skills make an agent hold back and ask instead of solving —
right for the Engineer persona, wrong for an autonomous agent. The engineer-archetype
bundle turns it on; by hand:

```yaml
plugins:
  enabled: [engineer]
```
