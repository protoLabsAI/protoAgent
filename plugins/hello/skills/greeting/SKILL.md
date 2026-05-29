---
name: greeting
description: >-
  Use when the user just wants a friendly greeting or to test that the example
  plugin is wired up. Demonstrates a plugin bundling a skill alongside its tool.
tools: [hello]
---

# Greeting

When the user asks for a greeting (or to check the example plugin), call the
`hello` tool with their name and relay the result warmly. This skill ships
*inside* the `hello` plugin to show that a plugin can contribute both tools and
skills together.
