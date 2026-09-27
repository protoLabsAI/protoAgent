- **Fleet Room composer moves onto the design system (#3683).** The broadcast bar's raw text
  input and send button are replaced by the DS `PromptInput` from `@protolabsai/ui/ai`, which
  now owns the field/send chrome. The @-mention / broadcast target renders as a `PromptInput`
  attachment chip above the field: a specific member is a removable chip (removing it clears the
  target back to broadcast) and the "Everyone else · N" fan-out is a read-only chip. Enter, ⌘↵
  (force broadcast) and the @-mention flow are unchanged. Part of #3683.
