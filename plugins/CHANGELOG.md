
## [chat_room] - 2025-12-06

### Added
- Created `plugins/chat_room` directory structure.
- Implemented Flask Blueprint `chat_room` with prefix `/chat`.
- Implemented chat backend with polling mechanism.
- Added `chat_messages` table to database schema.
- Added "在线聊天" menu item to "系统管理" dropdown.
- Implemented chat UI:
    - Group chat (Lobby) support.
    - Private chat (User-to-User) support.
    - User list display.
- Implemented AI integration:
    - Supports `@ai` command in chat to trigger AI response.
    - AI response is handled in a separate thread.
- Added Weather Card feature:
    - Supports `@ai天气[city]` command (e.g. `@ai天气[北京]`).
    - Integrates with external Weather API.
    - Renders structured weather forecast card in chat window.
