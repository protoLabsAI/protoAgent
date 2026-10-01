- **A rejected chat turn now shows as failed, and parked chats come back in a fresh browser (#3957).**
  The console classified task states in several places, and they had drifted apart: a turn
  the agent rejected was settled as done, and a turn whose state the server reported as
  unknown kept the chat busy for up to 10 minutes. All of these surfaces now use one shared
  classification. In a fresh browser profile, a chat waiting on an `ask_human` answer or an
  approval is now restored even when it is older than the 50 newest chats. The new
  `GET /api/chat/sessions?parked=true` lists those chats, and up to 20 of them are kept in
  the restored set.
