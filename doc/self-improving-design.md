# Self-Improving Design Architecture

> How telechat gets smarter over time — feedback loops, self-healing, auto-optimization, and knowledge accumulation.

## Overview

A self-improving system is one that monitors its own performance, learns from failures, accumulates knowledge, and upgrades itself — all with minimal human intervention. This document lays out the architecture for making telechat a continuously improving AI assistant.

The design follows four pillars:

| Pillar | What it does |
|--------|-------------|
| **Observe** | Collect signals from every interaction |
| **Evaluate** | Score quality automatically and via user feedback |
| **Learn** | Extract lessons and update behavior |
| **Heal** | Detect failures and recover autonomously |

---

## 1. Feedback Loop Architecture

The core self-improvement cycle runs continuously:

```
User Message
    |
    v
[Claude Generates Response]
    |
    v
[Deliver to User]
    |
    v
[Collect Signals] -----> [Evaluate Quality]
    |                          |
    v                          v
[Store Metrics]          [Diagnose Failures]
    |                          |
    v                          v
[Knowledge Store] <----- [Generate Lessons]
    |
    v
[Update System Prompt / Behavior]
    |
    v
[Next Interaction (improved)]
```

### Signal Collection

Every interaction produces signals at three levels:

**Explicit feedback:**
- Thumbs up/down reactions on messages (Telegram reactions, Slack emoji)
- `/rate` command for 1-5 star ratings
- `/feedback` command for free-text comments

**Implicit feedback:**
- User retries (sends same question again = bad response)
- User edits message and resends (= partial failure)
- Conversation abandoned after response (= possible failure)
- Follow-up asking for clarification (= unclear response)
- Long response time leading to user cancellation

**System signals:**
- Response latency (ms)
- Token usage (input/output)
- Error rate (API failures, timeouts)
- Tool call success/failure rate
- Session length and depth

### Implementation: Feedback Table

```sql
CREATE TABLE feedback (
    id INTEGER PRIMARY KEY,
    message_id INTEGER REFERENCES messages(id),
    platform TEXT NOT NULL,
    user_id TEXT NOT NULL,
    feedback_type TEXT NOT NULL,  -- 'reaction', 'rating', 'retry', 'abandon', 'error'
    value TEXT,                   -- '+1', '-1', '4', 'too verbose', etc.
    context TEXT,                 -- JSON: original prompt, response snippet
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE quality_scores (
    id INTEGER PRIMARY KEY,
    message_id INTEGER REFERENCES messages(id),
    scorer TEXT NOT NULL,         -- 'user', 'auto_length', 'auto_relevance', 'llm_judge'
    score REAL NOT NULL,          -- 0.0 to 1.0
    reasoning TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## 2. Automated Quality Evaluation

### LLM-as-Judge

After each response, a lightweight evaluation pass scores quality across dimensions:

| Dimension | What it measures | Auto-detectable? |
|-----------|-----------------|-----------------|
| Relevance | Does the response answer the question? | Yes (LLM judge) |
| Conciseness | Is it appropriately brief? | Yes (length ratio) |
| Accuracy | Are facts correct? | Partial (LLM judge) |
| Helpfulness | Did it solve the user's problem? | Partial (implicit signals) |
| Safety | No harmful content? | Yes (pattern match + LLM) |

### Scoring Implementation

```python
async def auto_evaluate(prompt: str, response: str, platform: str) -> dict:
    """Lightweight post-response quality check."""
    scores = {}

    # Length ratio check (response shouldn't be 10x the prompt for simple questions)
    prompt_words = len(prompt.split())
    response_words = len(response.split())
    if prompt_words < 20 and response_words > 500:
        scores["conciseness"] = 0.3  # likely too verbose
    else:
        scores["conciseness"] = min(1.0, 200 / max(response_words, 1))

    # Error pattern detection
    error_patterns = ["I cannot", "I'm unable", "I don't have access"]
    if any(p in response for p in error_patterns):
        scores["helpfulness"] = 0.4

    # Periodic deep evaluation (every Nth message, or on negative signals)
    if should_deep_evaluate():
        scores["relevance"] = await llm_judge_score(prompt, response)

    return scores
```

### Binary Evaluators

Cheap, fast checks that run on every response:

```python
BINARY_EVALS = [
    ("not_empty", lambda r: len(r.strip()) > 0),
    ("not_error", lambda r: not r.startswith("[Error]")),
    ("not_truncated", lambda r: not r.endswith("...")),
    ("reasonable_length", lambda r: 10 < len(r.split()) < 5000),
    ("no_hallucinated_links", lambda r: "http" not in r or validate_urls(r)),
]
```

---

## 3. Knowledge Accumulation

### Three-Layer Memory

```
+-----------------------+
|   Working Memory      |  <- Current conversation context
|   (per-session)       |     Cleared on /new
+-----------------------+
|   Episodic Memory     |  <- User preferences, past interactions
|   (per-user)          |     "User prefers concise answers"
+-----------------------+
|   Semantic Memory     |  <- Global lessons learned
|   (system-wide)       |     "When asked about code, include examples"
+-----------------------+
```

### User Profile Learning

```sql
CREATE TABLE user_preferences (
    user_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    preference_key TEXT NOT NULL,
    preference_value TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,    -- increases with repeated signals
    source TEXT NOT NULL,           -- 'explicit', 'inferred', 'feedback'
    updated_at TIMESTAMP,
    PRIMARY KEY (user_id, platform, preference_key)
);
```

Preferences are inferred from patterns:

| Signal | Inferred preference |
|--------|-------------------|
| User always asks to shorten responses | `response_style = concise` |
| User sends code snippets frequently | `context = developer` |
| User asks in Spanish | `language = es` |
| User upvotes responses with code blocks | `include_code = true` |

### System Prompt Evolution

The system prompt is built dynamically per-user:

```python
def build_system_prompt(user_id: str, platform: str) -> str:
    base = load_base_prompt()
    lessons = load_global_lessons()      # from learnings.md
    prefs = load_user_preferences(user_id, platform)

    prompt = base
    if lessons:
        prompt += f"\n\n## Learned Guidelines\n{lessons}"
    if prefs.get("response_style") == "concise":
        prompt += "\n\nKeep responses brief and to the point."
    if prefs.get("language"):
        prompt += f"\n\nRespond in {prefs['language']}."

    return prompt
```

### Global Lessons File (`learnings.md`)

A version-controlled file that accumulates operational lessons:

```markdown
# Telechat Learned Guidelines

## Response Quality
- When users ask "how to X", include a concrete example, not just explanation
- Limit responses to 300 words unless the user asks for detail
- For error messages, always suggest a fix, not just explain the error

## Platform-Specific
- Telegram: Keep messages under 4096 chars (API limit), split if needed
- WhatsApp: Avoid markdown formatting (not rendered), use plain text
- Slack: Use code blocks with language hints for syntax highlighting

## Common Failure Modes
- Don't apologize excessively — one brief acknowledgment is enough
- When asked about real-time data, state the knowledge cutoff clearly
- If a tool call fails, explain what went wrong in user-friendly terms
```

This file is:
- **Appended automatically** when the diagnostic loop identifies a new lesson
- **Reviewed periodically** by the maintainer to prune or merge entries
- **Injected into the system prompt** so Claude follows learned guidelines
- **Version-controlled** in git for auditability

---

## 4. Self-Healing Infrastructure

### Multi-Tier Recovery

```
Tier 1: Process-level auto-restart (systemd/launchd)
   |
   v  (if Tier 1 fails 3x in 5 minutes)
Tier 2: Watchdog health check (HTTP ping every 60s)
   |
   v  (if Tier 2 detects unresponsive)
Tier 3: Deep health diagnosis (check DB, API keys, network)
   |
   v  (if Tier 3 cannot fix)
Tier 4: Alert owner via Telegram/email
```

### Health Check Endpoints

```python
async def health_check() -> dict:
    checks = {}

    # Database
    try:
        db = get_db()
        db.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"

    # Claude API/CLI
    try:
        if CLAUDE_MODE == "api":
            # lightweight API ping
            checks["claude"] = "ok" if test_api_key() else "invalid_key"
        else:
            checks["claude"] = "ok" if claude_cli_available() else "not_installed"
    except Exception as e:
        checks["claude"] = f"error: {e}"

    # Platform connections
    for platform in PLATFORMS:
        checks[platform] = await check_platform_connection(platform)

    checks["uptime"] = time.time() - START_TIME
    checks["memory_mb"] = get_memory_usage()
    checks["status"] = "healthy" if all(v == "ok" for v in checks.values() if v != checks["uptime"]) else "degraded"

    return checks
```

### Circuit Breaker Pattern

```python
class CircuitBreaker:
    def __init__(self, failure_threshold=5, reset_timeout=60):
        self.failures = 0
        self.threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.state = "closed"  # closed=normal, open=failing, half-open=testing
        self.last_failure_time = 0

    async def call(self, func, *args, **kwargs):
        if self.state == "open":
            if time.time() - self.last_failure_time > self.reset_timeout:
                self.state = "half-open"
            else:
                raise CircuitOpenError("Service unavailable, retrying soon")

        try:
            result = await func(*args, **kwargs)
            if self.state == "half-open":
                self.state = "closed"
                self.failures = 0
            return result
        except Exception as e:
            self.failures += 1
            self.last_failure_time = time.time()
            if self.failures >= self.threshold:
                self.state = "open"
                log.error("Circuit breaker OPEN after %d failures", self.failures)
            raise

# Usage
claude_breaker = CircuitBreaker(failure_threshold=3, reset_timeout=30)
response = await claude_breaker.call(ask_claude, prompt, history)
```

### Graceful Degradation

When Claude API is down, the system degrades instead of crashing:

| Failure | Degradation |
|---------|------------|
| Claude API timeout | Retry with exponential backoff (1s, 2s, 4s) |
| Claude API down | Switch to CLI mode if available |
| CLI mode down | Return "I'm temporarily unavailable" message |
| Database corrupt | Switch to in-memory storage, alert owner |
| Platform API rate limited | Queue messages, deliver when limit resets |

---

## 5. Auto-Update Mechanism

### Version Check on Startup

```python
async def check_for_updates():
    """Check PyPI for newer version on startup."""
    try:
        current = __version__
        response = await httpget(f"https://pypi.org/pypi/telechatai/json")
        latest = response["info"]["version"]
        if version.parse(latest) > version.parse(current):
            log.info("Update available: %s -> %s", current, latest)
            notify_owner(f"Telechat update available: {current} -> {latest}\n"
                        f"Run: pip install --upgrade telechatai")
    except Exception:
        pass  # non-critical
```

### Safe Update Pipeline

```
1. Check for update (startup + daily)
        |
2. Download new version to staging
        |
3. Run smoke tests against staging
        |
4. If tests pass → notify owner
        |
5. Owner approves → apply update
        |
6. Health check after update
        |
7. If unhealthy → auto-rollback
```

---

## 6. Prompt Self-Optimization

### The Optimization Loop

```
[Collect 50+ rated interactions]
        |
        v
[Group by quality score: good vs. bad]
        |
        v
[Analyze patterns in bad responses]
        |
        v
[Generate candidate prompt improvements]
        |
        v
[A/B test: 90% current prompt, 10% candidate]
        |
        v
[After 50 interactions on candidate, compare scores]
        |
        v
[If candidate wins → promote to default]
[If candidate loses → discard, try next]
```

### Prompt Versioning

```sql
CREATE TABLE prompt_versions (
    id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL,
    prompt_text TEXT NOT NULL,
    avg_score REAL DEFAULT 0.0,
    total_uses INTEGER DEFAULT 0,
    positive_feedback INTEGER DEFAULT 0,
    negative_feedback INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT FALSE,
    is_candidate BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    promoted_at TIMESTAMP
);
```

### Diagnostic Feedback Generation

When a response scores poorly:

```python
async def diagnose_failure(prompt: str, response: str, feedback: str) -> str:
    """Use Claude to diagnose why a response was bad."""
    diagnosis_prompt = f"""
    A user sent this message: {prompt[:200]}
    The bot responded: {response[:500]}
    The user's feedback: {feedback}

    What specific guideline should the bot follow to avoid this
    kind of failure in the future? Write one clear, actionable rule.
    """
    lesson = await ask_claude_for_diagnosis(diagnosis_prompt)
    append_to_learnings(lesson)
    return lesson
```

---

## 7. Observability & Telemetry

### Key Metrics Dashboard

```
Daily Report (auto-generated):
  Messages processed: 142
  Avg response time:  2.3s
  Error rate:         1.4%
  User satisfaction:  4.2/5 (from 23 ratings)
  Token usage:        45,230 input / 38,100 output
  Cost estimate:      $0.42
  Top failure modes:  timeout (2), too_verbose (3)
  Lessons learned:    1 new guideline added
```

### Anomaly Detection

```python
def detect_anomalies(metrics: dict) -> list[str]:
    alerts = []
    # Response time spike
    if metrics["avg_response_time"] > 2 * metrics["baseline_response_time"]:
        alerts.append("Response time 2x above baseline")
    # Error rate spike
    if metrics["error_rate"] > 0.05:
        alerts.append(f"Error rate at {metrics['error_rate']:.1%}")
    # Satisfaction drop
    if metrics["avg_rating"] < metrics["baseline_rating"] - 0.5:
        alerts.append("User satisfaction dropped significantly")
    # Cost spike
    if metrics["daily_cost"] > metrics["budget_limit"]:
        alerts.append(f"Daily cost ${metrics['daily_cost']:.2f} exceeds budget")
    return alerts
```

---

## 8. Plugin Architecture for Community Extensions

### Extension Points

```python
# telechat_pkg/plugins.py

class TelechatPlugin:
    """Base class for telechat plugins."""
    name: str = "unnamed"
    version: str = "0.1.0"

    async def on_message(self, message: str, context: dict) -> str | None:
        """Pre-process incoming message. Return modified message or None."""
        return None

    async def on_response(self, response: str, context: dict) -> str | None:
        """Post-process outgoing response. Return modified response or None."""
        return None

    async def on_feedback(self, feedback: dict) -> None:
        """React to user feedback."""
        pass

    def get_commands(self) -> dict[str, callable]:
        """Register custom slash commands."""
        return {}
```

### Plugin Discovery

```
~/.telechat/plugins/
    translation/
        __init__.py      # class TranslationPlugin(TelechatPlugin)
    summarizer/
        __init__.py      # class SummarizerPlugin(TelechatPlugin)
    custom_tools/
        __init__.py      # class CustomToolsPlugin(TelechatPlugin)
```

---

## 9. Implementation Roadmap

### Phase 1: Foundation (v1.x)
- [x] SQLite conversation history
- [x] Per-user rate limiting
- [x] Multi-platform support
- [ ] Feedback collection (reactions + /rate command)
- [ ] Binary quality evaluators
- [ ] Basic health check endpoint
- [ ] Watchdog with auto-restart

### Phase 2: Learning (v2.x)
- [ ] User preference inference
- [ ] Global learnings.md injection into system prompt
- [ ] LLM-as-judge periodic evaluation
- [ ] Daily metrics report (sent to owner via bot)
- [ ] Anomaly detection alerts
- [ ] Circuit breaker for Claude API

### Phase 3: Self-Optimization (v3.x)
- [ ] Prompt versioning and A/B testing
- [ ] Automated diagnostic feedback loop
- [ ] Safe prompt promotion pipeline
- [ ] Auto-update notifications
- [ ] Plugin architecture

### Phase 4: Autonomous (v4.x)
- [ ] Multi-tier self-healing
- [ ] Automated prompt optimization (no human approval needed)
- [ ] Community plugin marketplace
- [ ] Cross-instance learning (opt-in aggregated improvements)

---

## References

- [Self-Evolving Agents Cookbook (OpenAI)](https://developers.openai.com/cookbook/examples/partners/self_evolving_agents/autonomous_agent_retraining)
- [How to Build a Self-Improving AI Agent (MindStudio)](https://www.mindstudio.ai/blog/self-improving-ai-agent-feedback-loop)
- [7 Tips for Self-Improving AI Agents (Datagrid)](https://datagrid.com/blog/7-tips-build-self-improving-ai-agents-feedback-loops)
- [Self-Healing AI Agent System: 70+ Production Bugs (DEV)](https://dev.to/_d7eb1c1703182e3ce1782/how-to-build-a-self-healing-ai-agent-system-lessons-from-70-production-bugs-2nep)
- [AI Agent Architecture (Redis)](https://redis.io/blog/ai-agent-architecture/)
- [Agentic AI Design Patterns (Google Cloud)](https://docs.cloud.google.com/architecture/choose-design-pattern-agentic-ai-system)
- [Prompt Learning: Feedback-Driven Optimization (Arize AI)](https://arize.com/blog/prompt-learning-using-english-feedback-to-optimize-llm-systems/)
- [LLMOps for AI Agents in Production (OneReach)](https://onereach.ai/blog/llmops-for-ai-agents-in-production/)
- [Self-Improving AI with Claude Code and Cowork](https://www.productcompass.pm/p/self-improving-claude-system)
