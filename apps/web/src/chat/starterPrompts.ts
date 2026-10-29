export const STARTER_PROMPTS: readonly { label: string; prompt: string; note?: string }[] = [
  {
    label: "Take a quick tour",
    prompt: "What can you do? Give me a quick tour of your tools.",
  },
  {
    label: "Summarise a web page",
    prompt: "Summarise this web page for me: https://agent.protolabs.studio",
  },
  {
    label: "Remember my preferences",
    prompt: "Remember that I prefer short answers, then tell me what you remember about me.",
  },
  {
    label: "Ask Claude Code to add a test",
    prompt: "Ask Claude Code to add a test to a small Python function.",
    note: "Needs Claude Code installed",
  },
];
