- **Model roles get a chain editor in Settings.** A `model_chain` field now renders one
  ordered control (ADR 0133 M7b): a Combobox row per model — "Primary" then "then" — with
  ↑ / ↓ / ✕ and a "+ Add fallback" button, and a live region that announces each reorder.
  A blank inheriting role shows one quiet "Inherits <parent>" line whose toggletip opens a
  popover listing the parent's full chain (including unsaved edits); "Set its own chain"
  pre-fills that chain and "Use <parent>'s chain" clears it. The main chain can't be emptied
  and folds any legacy `routing.fallback_models` in after its primary on the next save. An
  entry on no connection is kept, with a "will be skipped" warning. Below 768px each row
  becomes a card that wraps long model ids across their own lines so nothing is ever clipped.
