# Advanced Telegram Features for Telechat

Research on advanced Telegram Bot API features to enhance Claude CLI/SDK chatbot experience.

---

## Tier 1: High Impact, Implement Now

### 1. Streaming Text Responses (Bot API 2026)

Telegram now supports **streaming text** — bots can stream responses as they generate, rather than waiting for the full message. This is the single most impactful feature for an AI chatbot.

**Current approach:** Edit placeholder message periodically with partial text.
**New approach:** Use native streaming API for smoother word-by-word display with built-in animations.

**Implementation:** Already partially implemented via `TaskSession.on_text()` editing the placeholder. Can be improved with faster edit intervals and MarkdownV2 formatting.

### 2. Voice Message Input (Speech-to-Text)

Users send voice notes → bot transcribes → sends to Claude → responds with text.

**API:** `message.voice` contains an OGG file. Download via `bot.get_file()`.
**Transcription options:**
- OpenAI Whisper API (`openai.audio.transcriptions.create`)
- Local Whisper model (no API key, privacy-friendly, CPU-only)
- Anthropic's own audio support (if available in Claude API)

**Flow:**
```
User sends voice → download .ogg → convert to .wav (ffmpeg) → 
  Whisper transcribe → send text to Claude → reply
```

**python-telegram-bot handler:**
```python
app.add_handler(MessageHandler(filters.VOICE, handle_voice))
```

### 3. MarkdownV2 Formatting

Current bot uses legacy Markdown which breaks on special characters. MarkdownV2 supports:
- `||spoiler text||` — spoiler
- `> blockquote` and `**>` expandable blockquote
- `` ```python\ncode``` `` — language-specific code blocks
- `__underline__`, `~~strikethrough~~`
- Custom emoji via `![emoji](tg://emoji?id=12345)`

**Impact:** Claude responses with code blocks will render with syntax highlighting hints. Blockquotes improve readability for quoted content.

**Migration:** Escape these chars in MarkdownV2: `_*[]()~` `` ` `` `>#+-=|{}.!`

### 4. Message Reactions

Let users react to Claude's responses with emoji. Track reactions as implicit feedback.

**API:** `setMessageReaction(chat_id, message_id, reaction=[ReactionTypeEmoji(emoji="👍")])`
**Event:** `MessageReactionUpdated` — fires when user adds/removes reaction.

**Use for self-improving system:**
- 👍/❤️ → positive feedback (rating 5)
- 👎 → negative feedback (rating 1)
- 🔥 → exceptionally good (save to learnings)
- 🤔 → confusing response (flag for review)

**Handler:**
```python
app.add_handler(MessageReactionHandler(handle_reaction))
```

### 5. Guest Bot Mode (Bot API 2026)

Users can **@mention your bot in any chat** — even where the bot isn't a member. The bot only sees the specific message it's tagged in.

**Impact:** Users can invoke Claude from any group chat by typing `@telechat_bot explain this code`.

**API:** Receives `guest_message` update → respond via `answerGuestQuery`.

**Implementation:** Enable via BotFather settings. Add handler for guest queries.

### 6. Inline Mode (@bot queries from any chat)

Users type `@telechat_bot <query>` in any chat's input field. Bot returns results that the user can send inline.

**Use cases:**
- Quick Claude answers without switching chats
- Code generation snippets
- Translation on the fly
- Fact lookup

**API:** `InlineQueryHandler` → return `InlineQueryResultArticle` with Claude's response.

**Setup:** `/setinline` via BotFather, set placeholder text like "Ask Claude anything..."

```python
async def handle_inline(update, ctx):
    query = update.inline_query.query
    if len(query) < 3: return
    # Quick Claude response (short, API mode for speed)
    reply = await quick_claude(query)
    results = [InlineQueryResultArticle(
        id="1", title="Claude's answer",
        input_message_content=InputTextMessageContent(reply),
        description=reply[:100]
    )]
    await update.inline_query.answer(results, cache_time=0)
```

---

## Tier 2: Medium Impact, Worth Adding

### 7. Bot Commands Menu

Register commands with Telegram so they appear in the "/" menu with descriptions.

**API:** `setMyCommands` with scope for different contexts.

```python
commands = [
    BotCommand("start", "Start the bot"),
    BotCommand("rate", "Rate last response (1-5)"),
    BotCommand("quality", "View quality metrics"),
    BotCommand("sessions", "Manage sessions"),
    BotCommand("browse", "Browse project files"),
    BotCommand("model", "Switch Claude model"),
    BotCommand("engine", "Switch engine (cli/sdk/api)"),
    BotCommand("tasks", "Show active tasks"),
    BotCommand("cancel", "Cancel a task"),
    BotCommand("reset", "Clear history"),
    BotCommand("usage", "Usage statistics"),
    BotCommand("mode", "Current settings"),
    BotCommand("id", "Show your Telegram ID"),
]
await app.bot.set_my_commands(commands)
```

### 8. Reply Keyboard for Common Actions

Show persistent buttons below the input field for frequent actions.

**Use case:** After a response, show:
- [👍 Good] [👎 Bad] [🔄 Retry] [📋 Copy]

**API:** `ReplyKeyboardMarkup` with `resize_keyboard=True`, `one_time_keyboard=True`.

Better UX than inline buttons for quick feedback since they don't clutter the message.

### 9. Send Long Responses as Documents

When Claude generates very long responses (>4096 chars), instead of paginating:
- Send as a `.md` or `.txt` file attachment
- Include a summary in the message text

**API:** `send_document(chat_id, document=BytesIO(text.encode()), filename="response.md")`

**Threshold:** If response > 8000 chars, offer both paginated view and file download.

### 10. Video Notes (Round Videos)

For responses that would benefit from visual explanation, Claude could generate a response that gets rendered as a short video note (round video).

More practically: accept user video notes as input, extract frames for vision analysis.

**API:** `message.video_note` → download, extract keyframes, send to Claude with vision.

### 11. Message Effects

Send responses with animated effects for special occasions.

**API:** `send_message(chat_id, text, message_effect_id="...")`

**Use case:** Use fire effect for "exceptional" responses, or celebration effect when completing complex tasks.

### 12. Sticker Responses

React with contextual stickers for status updates:
- Thinking sticker while processing
- Celebration sticker on task completion
- Error sticker on failures

**API:** `send_sticker(chat_id, sticker=file_id)`

### 13. Forum Topics Support

In supergroups with topics enabled, route conversations to specific topics.

**Use case:** Each session maps to a forum topic. Users can have parallel Claude conversations in different topics.

**API:** `create_forum_topic(chat_id, name)` → use `message_thread_id` in replies.

---

## Tier 3: Advanced / Future

### 14. Mini App (Web App)

Build a rich web UI that opens inside Telegram for:
- Code editor with syntax highlighting
- File browser with tree view
- Settings panel with all configuration
- Quality dashboard with charts
- Session manager with drag-and-drop

**API:** `WebAppInfo(url="https://telechat.app/miniapp")` in keyboard button.

**Complexity:** Requires hosting a web app. Best for v2.0.

### 15. Bot-to-Bot Communication

Chain multiple bots: telechat bot calls a specialized code-review bot, or a search bot.

**API:** Bots can now send messages to other bots by username.

**Use case:** Modular architecture — separate bots for different capabilities.

### 16. Chat Automation (Act on User's Behalf)

Users can connect telechat to their profile, allowing it to respond to messages automatically.

**Impact:** Claude as a personal assistant that answers messages when you're busy.

**API:** Configurable per-chat-type access controls.

### 17. Payments / Telegram Stars

Accept payments for premium features (higher rate limits, priority processing, opus model access).

**API:** `sendInvoice`, `LabeledPrice`, handle `pre_checkout_query`.

### 18. ConversationHandler (python-telegram-bot)

Multi-step flows with state management:
- Guided setup wizard within Telegram
- Step-by-step feedback collection
- Interactive file editing workflow

**Library:** `ConversationHandler(entry_points, states, fallbacks, conversation_timeout)`

---

## Implementation Priority

| Priority | Feature | Effort | Impact |
|----------|---------|--------|--------|
| P0 | Voice message input | Medium | Very High |
| P0 | MarkdownV2 migration | Medium | High |
| P0 | Bot commands menu | Low | High |
| P1 | Message reactions → feedback | Low | High |
| P1 | Inline mode | Medium | High |
| P1 | Guest bot mode | Low | Medium |
| P1 | Long responses as files | Low | Medium |
| P2 | Reply keyboard feedback | Low | Medium |
| P2 | Streaming improvements | Medium | Medium |
| P2 | Forum topics | Medium | Medium |
| P3 | Mini App | High | High |
| P3 | Video note input | Medium | Low |
| P3 | Payments | Medium | Low |

---

## Sources

- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Telegram Bot API Changelog](https://core.telegram.org/bots/api-changelog)
- [Telegram Inline Bots](https://core.telegram.org/bots/inline)
- [Telegram Mini Apps](https://core.telegram.org/bots/webapps)
- [AI Bot Revolution — 11 New Features](https://telegram.org/blog/ai-bot-revolution-11-new-features)
- [Bot API 2026 for AI Agents](https://zeroclaws.io/blog/telegram-bot-api-2026-ai-agent-developers-guide)
- [python-telegram-bot v22.7 docs](https://docs.python-telegram-bot.org/)
- [tg-ai-bot reference implementation](https://github.com/TokenMixAi/tg-ai-bot)
