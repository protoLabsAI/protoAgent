- **Demo-agent settings (#4609).** New `tools.fleet_demo.enabled` (default off) gates the
  demo-agent tools, with `tools.fleet_demo.max_live` (3) capping live demo members and
  `tools.fleet_demo.ttl_hours` (24) aging them out. All three surface in Settings ▸ Tools, and
  the two limits flow into the demo create/reap path. The tools only ever touch members they
  created (stamped `demo: true`), never a real fleet agent.
