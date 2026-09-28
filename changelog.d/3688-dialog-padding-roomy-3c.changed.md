- **Console: mcp-catalog, docviewer, knowledge & memory content dialogs opt into DS 0.63 `<Dialog padding="roomy">` (#3688).**
  The common-MCP-server catalog dialog (`McpCatalogDialog`), the document reader
  (`DocumentViewer`), both knowledge-store dialogs — add-source and add-entry
  (`KnowledgeStore`) — and the memory injection-detail dialog (`MemorySurface`) now pass
  `padding="roomy"`, which `@protolabsai/ui` 0.63.0 renders as `.pl-dialog__body--roomy`
  (24px). While the app-wide `.pl-dialog__body { padding: 24px }` rule still exists these
  dialogs render identically; the opt-in is what preserves their 24px body padding once a
  later card deletes that global rule (the DS default is 16px). No CSS changes. Part of
  #3688 (DS 0.63 card 3, Dialog body padding).
