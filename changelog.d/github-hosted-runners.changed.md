- **Scaffolded plugins run CI on GitHub-hosted runners.** The plugin scaffold (`plugin new`) now
  generates a CI workflow with `runs-on: ubuntu-latest` instead of a Namespace runner
  profile, the desktop build's runner comments describe GitHub-hosted runners only, and
  the customize-and-deploy guide states the GitHub-hosted runner policy.
