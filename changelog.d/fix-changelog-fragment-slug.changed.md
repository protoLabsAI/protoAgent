- **A changelog fragment can be written correctly in a PR's first commit.** Name it
  `changelog.d/<slug>.<kind>.md`, using your branch name or the issue number, and leave
  the PR number out of the bold lead. At release, `changelog.py collate` adds the number
  to any lead that doesn't cite one, reading it from the squash-merge commit that added
  the fragment. Before this, a PR with no issue had to push a second commit just to add
  its own number, and each push ran another full review: 26 PRs and 17 cancelled review
  panels in two weeks (#4874). The `Changelog entry` check now rejects a placeholder such
  as `(#NNNN)`, a bullet with no bold lead and no `#N`, and a name that isn't a slug.
