- **Every device now shows the same chat tabs.** The chat tab list is server-authoritative
  (ADR 0130 S6): creating, renaming, closing or deleting a chat on one device now shows on all
  the others within a couple of seconds, and a chat's title is held by the server rather than
  only in the browser that named it. Closing a tab closes it everywhere — the chat stays
  retrievable, it isn't deleted — and a client's existing tabs migrate to the server once so none
  are lost. Previously the desktop and the phone could show entirely different chats.
