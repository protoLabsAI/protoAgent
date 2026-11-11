- **The `code-review` workflow can take the board card a PR implements.** A new optional `card`
  input (title, task, acceptance criteria, starting files) reaches every finder and the
  synthesizer, fenced as data. A defect in changed code stays in scope whatever the card says.
  A finding that only asks the PR to do more than the card asks for is a minor `beyond-card`
  finding, never a blocker. projectBoard's review gate sends it (projectBoard-plugin#721); a run
  without a card behaves as before.
