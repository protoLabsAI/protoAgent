- **Other conversations carry who started them.** Each `GET /api/conversations` row now
  includes the display-only `caller`, `caller_kind` and `on_behalf_of` recorded on the thread's
  first turn, so the Conversations list can show who a thread is from and on whose behalf. Absent
  or unrecognised fields are omitted; they are never an authorization input.
