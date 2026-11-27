- **Your name and organization are set once for the whole machine, and every agent is told who it works for.**
  Settings → Operator & access → Operator and Organization are now box-wide: set them on the
  main instance and every agent on this machine inherits them, and each agent's prompt gets one
  line ("You work for Josh at protoLabs."). Before, Operator was saved for one agent only and no
  prompt ever used it. On upgrade, a name already saved on the main instance moves up to the
  machine by itself. An agent can still set its own, or blank Operator to get no line.
  Settings also says where each value comes from only when that tells you something: "From this
  machine", "Set for this agent" (with what the machine uses and a "Use machine value" button),
  "Every agent on this machine" (with which agents set their own), or "Built-in". An empty
  setting nobody set no longer reads "inherited from default". Fixes #5812.
