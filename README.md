# LLM Reference Pattern — Multi-Agent Bicep Processing

A reference architecture demonstrating a **multi-agent orchestration pattern** using [Microsoft Agent Framework](https://pypi.org/project/agent-framework/) and Azure AI Foundry. The system reads Azure Bicep infrastructure files from a remote GitHub repository, applies best-practice improvements via LLM-powered agents, and commits the modified file back — all without touching the local filesystem.

Four processing modes are supported, selectable via an environment variable, to illustrate the trade-off between simplicity and token efficiency.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  main.py                                                    │
│  ┌───────────────────────────┐                              │
│  │   MasterOrchestratorAgent │                              │
│  │                           │                              │
│  │  Parses user request      │                              │
│  │  Resolves repo/branch     │                              │
│  │  from env vars            │                              │
│  │                           │                              │
│  │  Tool:                    │                              │
│  │  process_bicep_with_      │    Delegates via             │
│  │  infra_prep(file_path,────┼──► agent.run(prompt)         │
│  │    repo, branch)          │                              │
│  └───────────────────────────┘                              │
│                                                             │
│  ┌───────────────────────────┐    ┌──────────────────────┐  │
│  │     InfraPrepAgent        │    │    GitHub (remote)    │  │
│  │                           │    │                      │  │
│  │  Mode: full / chunked /   │◄──►│  read / write files  │  │
│  │  batched / isolated_batch  │    │  via PyGithub API    │  │
│  │  (set via env var)        │    │                      │  │
│  │  Tools: bicep_tools.py    │    └──────────────────────┘  │
│  └───────────────────────────┘                              │
└─────────────────────────────────────────────────────────────┘
```

### Agent Hierarchy

| Agent | Role | LLM Calls |
|---|---|---|
| **MasterOrchestratorAgent** | Parses user intent, resolves defaults from env vars, delegates to InfraPrepAgent, reports results | 2 (decide to call tool + format final response) |
| **InfraPrepAgent** | Reads Bicep from GitHub, applies best-practice modifications, commits back | Varies by mode (see below) |

---

## How References Are Passed Between Agents (Token Efficiency)

A critical design choice is **how the MasterOrchestratorAgent communicates with the InfraPrepAgent**. File contents are never passed between agents — only **references** (file path, repo name, branch):

```
MasterAgent                          InfraPrepAgent
    │                                      │
    │  "Process 'infra/main.bicep'         │
    │   in repo 'owner/repo'               │
    │   on branch 'main'"                  │
    ├─────────────────────────────────────►│
    │        (text prompt — ~50 tokens)     │
    │                                      │
    │                               ┌──────┤
    │                               │ InfraPrepAgent calls
    │                               │ GitHub tools directly
    │                               │ (file content stays
    │                               │  inside tool calls,
    │                               │  never sent to Master)
    │                               └──────┤
    │                                      │
    │  "File created on GitHub:            │
    │   https://github.com/..."            │
    │◄─────────────────────────────────────┤
    │        (URL string — ~30 tokens)     │
    │                                      │
```

**Why this matters:**

- The Bicep file (~10,000 tokens for a 1,300-line file) is read and written entirely within the InfraPrepAgent's tool calls.
- The MasterOrchestratorAgent only ever sees the **file path** in its prompt and the **GitHub URL** in the response — never the file contents.
- This means the MasterAgent's token usage stays constant (~1,000-1,500 tokens) regardless of how large the Bicep file is.
- If the file content were returned to the MasterAgent, every subsequent LLM call would include it in the conversation history, compounding cost.

---

## Processing Modes

### Full Mode (`BICEP_PROCESSING_MODE=full`)

The default mode. The InfraPrepAgent reads the entire Bicep file, sends it to the LLM, and the LLM produces the entire modified file as output.

```
InfraPrepAgent (full mode)
    │
    │  1. read_bicep_file(repo, path, branch)
    │     → LLM receives entire file content (~10K tokens input)
    │
    │  2. LLM reasons about changes, outputs entire modified file
    │     → modify_bicep_file(repo, path, full_content, branch)
    │     → (~11K tokens output)
    │
    │  Total: ~42K tokens (input accumulates across round-trips)
```

**Tools used:**

| Tool | Description |
|---|---|
| `read_bicep_file` | Reads entire file from GitHub, returns full content to the LLM |
| `modify_bicep_file` | LLM provides full modified content; tool commits to GitHub with `_modified_<datetime>` suffix |

**Characteristics:**
- Simple — 2 tool calls
- Higher token usage — the LLM must read and regenerate the entire file
- Context window accumulates: system prompt + tool defs + file content + conversation history
- Best for small files or when simplicity is preferred

### Chunked Mode (`BICEP_PROCESSING_MODE=chunked`)

The token-optimized mode. The file is parsed into logical sections (resources, parameter groups, variable groups, etc.) and the LLM only retrieves and modifies the chunks that need changes.

```
InfraPrepAgent (chunked mode)
    │
    │  1. read_bicep_structure(repo, path, branch)
    │     → Returns chunk SUMMARY only (type, name, line count)
    │     → LLM sees ~500 tokens, NOT the file content
    │
    │  2. LLM identifies which chunks need modification
    │     (skips compliant chunks entirely)
    │
    │  3. For each chunk needing changes:
    │     a. get_bicep_chunk(index)    → retrieves one section
    │     b. LLM modifies it
    │     c. update_bicep_chunk(index, modified_content) → stores it
    │
    │  4. commit_bicep_chunks()
    │     → Reassembles all chunks (modified + original)
    │     → Commits to GitHub with _modified_<datetime> suffix
    │
    │  Total: ~10-15K tokens (only chunks needing changes)
```

**Tools used:**

| Tool | Description |
|---|---|
| `read_bicep_structure` | Reads file from GitHub, parses into chunks, returns **summary only** (no content) |
| `get_bicep_chunk(index)` | Returns the content of a single chunk |
| `update_bicep_chunk(index, content)` | Stores modified content for a chunk (server-side, not sent back to LLM) |
| `commit_bicep_chunks` | Reassembles modified + unmodified chunks, commits to GitHub |

**Characteristics:**
- More tool calls but significantly fewer tokens per call
- LLM context window stays small — only one chunk at a time
- Chunks that already comply with best practices are never loaded
- The `BicepChunkManager` (Python, server-side) stores state — chunk reassembly happens in code, not in the LLM
- Best for large files where token cost matters

### Batched Mode (`BICEP_PROCESSING_MODE=batched`)

Like chunked mode, but retrieves and updates multiple chunks per tool call to reduce the number of LLM round-trips.

```
InfraPrepAgent (batched mode)
    │
    │  1. read_bicep_structure(repo, path, branch)
    │     → Returns chunk SUMMARY only
    │
    │  2. LLM identifies chunks needing changes, groups into batches of ~5
    │
    │  3. For each batch:
    │     a. get_bicep_chunks_batch("2,5,7,9,12") → retrieves 5 chunks at once
    │     b. LLM modifies all 5
    │     c. update_bicep_chunks_batch([{index, content}, ...]) → stores all 5
    │
    │  4. commit_bicep_chunks()
    │     → Reassembles and commits to GitHub
    │
    │  Total: ~65K tokens (fewer round-trips, but history still accumulates)
```

**Tools used:**

| Tool | Description |
|---|---|
| `read_bicep_structure` | Reads file, returns summary only |
| `get_bicep_chunks_batch(indices)` | Returns content of multiple chunks in one call (comma-separated indices) |
| `update_bicep_chunks_batch(updates)` | Stores multiple modified chunks in one call (JSON array of `{index, content}`) |
| `commit_bicep_chunks` | Reassembles and commits to GitHub |

**Characteristics:**
- Fewer round-trips than chunked (~10 vs ~35)
- Conversation history still accumulates — each batch's content stays in context
- The batch tool arguments (full modified content as JSON) are large, causing high cumulative input
- Better than chunked for round-trip count, but similar or worse for total tokens due to larger payloads

### Isolated Batched Mode (`BICEP_PROCESSING_MODE=isolated_batched`)

The most token-efficient mode. Each processing step runs as an **independent agent with a fresh context** — no conversation history carries over between steps. State is maintained server-side in Python memory via the `BicepChunkManager` singleton.

```
Step 1: InfraPrepAnalyzer (fresh agent.run)
    │  read_bicep_structure → returns chunk summary
    │  LLM returns: "2,5,7,9,12,15,18,20,22,25,28,30"
    │  (only indices, no file content)
    │  Context is discarded after this run.

Step 2a: InfraPrepModifier (fresh agent.run)
    │  get_bicep_chunks_batch("2,5,7,9,12") → 5 chunks
    │  LLM modifies → update_bicep_chunks_batch([...])
    │  Context is discarded after this run.

Step 2b: InfraPrepModifier (fresh agent.run)
    │  get_bicep_chunks_batch("15,18,20,22,25") → 5 chunks
    │  (NO history from previous batch)
    │  Context is discarded after this run.

Step 2c: InfraPrepModifier (fresh agent.run)
    │  get_bicep_chunks_batch("28,30") → 2 chunks
    │  Context is discarded after this run.

Step 3: InfraPrepCommitter (fresh agent.run)
    │  commit_bicep_chunks() → reassemble + push to GitHub
    │  Returns GitHub URL
    │
    │  Total: ~18-25K tokens (no history accumulation)
```

**Sub-agents:**

| Agent | Role | Tools | Context |
|---|---|---|---|
| `InfraPrepAnalyzer` | Read structure, identify chunk indices needing changes | `read_bicep_structure` | Fresh |
| `InfraPrepModifier` | Get a batch of chunks, modify, store (one run per batch) | `get_bicep_chunks_batch`, `update_bicep_chunks_batch` | Fresh per batch |
| `InfraPrepCommitter` | Reassemble and commit to GitHub | `commit_bicep_chunks` | Fresh |

**Characteristics:**
- Each step gets a fresh LLM context — no history accumulation
- The `BicepChunkManager` singleton keeps state in Python memory across independent runs
- Lowest total token usage of all modes (~18-25K for a 1,300-line file)
- Token usage per batch is constant (~5K), not growing
- The orchestration logic lives in `master_agent.py` rather than the LLM, making it deterministic
- Trade-off: the modifier agent has no cross-batch context (can't reference changes made in a previous batch)

### How Chunking Works

The `BicepChunkManager` parser splits a Bicep file at logical boundaries:

1. **Preamble** — file header comments
2. **Parameter groups** — consecutive `param` declarations merged into one chunk
3. **Variable groups** — consecutive `var` declarations merged into one chunk
4. **Resources** — each `resource` block is its own chunk
5. **Output groups** — consecutive `output` declarations merged into one chunk

Consecutive declarations of the same type (e.g., 15 parameters) are merged into a single chunk to avoid excessive granularity.

### Token Usage Comparison

For a ~1,336-line enterprise Bicep file:

| | Full Mode | Chunked Mode | Batched Mode | Isolated Batched |
|---|---|---|---|---|
| **Input tokens** | ~31,000 | ~60,000–80,000 | ~56,000 | ~15,000–20,000 |
| **Output tokens** | ~11,000 | ~15,000 | ~8,000 | ~8,000 |
| **Total tokens** | ~42,000 | ~75,000–95,000 | ~65,000 | ~18,000–25,000 |
| **LLM round-trips** | 3 | ~35 | ~10 | ~8 (isolated) |
| **Cached tokens** | ~19,000 | Moderate | Moderate | Minimal (fresh contexts) |
| **History accumulation** | Yes | Yes (high) | Yes (high) | **No** |

> **Note**: Chunked and batched modes have higher _cumulative_ input tokens because the conversation
> history grows with each tool call within a single `agent.run()`. Isolated batched avoids this
> by running each step as an independent agent with a fresh context.

---

## Setup

### Prerequisites

- Python 3.11+
- An Azure AI Foundry project with a deployed model (e.g., `gpt-5.1`)
- Azure CLI authenticated (`az login`)
- A GitHub personal access token with `repo` scope

### Install

```bash
cd llm-ref-pattern
python -m venv .venv
.venv\Scripts\activate       # Windows
# source .venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
```

### Configure

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description | Required |
|---|---|---|
| `AZURE_AI_PROJECT_ENDPOINT` | Azure AI Foundry project endpoint | Yes |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | Model deployment name (e.g. `gpt-5.1`) | Yes |
| `GITHUB_TOKEN` | GitHub PAT with `repo` scope | Yes |
| `GITHUB_REPO` | Target repository in `owner/repo` format | Yes |
| `GITHUB_BRANCH` | Branch to commit to (default: `main`) | No |
| `BICEP_PROCESSING_MODE` | `full`, `chunked`, `batched`, or `isolated_batched` (default: `full`) | No |

### Run

```bash
cd src
python main.py
```

---

## Project Structure

```
llm-ref-pattern/
├── .env.example                   # Environment variable template
├── requirements.txt               # Python dependencies
├── infra/
│   └── main.bicep                 # Sample enterprise Bicep file
└── src/
    ├── main.py                    # Entry point — creates agents, runs orchestration
    ├── agents/
    │   ├── master_agent.py        # MasterOrchestratorAgent — parses intent, delegates
    │   └── infra_prep_agent.py    # InfraPrepAgent — mode switch, Bicep modification logic
    └── tools/
        └── bicep_tools.py         # All GitHub I/O tools + BicepChunkManager
```

---

## Logging

The system provides verbose logging at every stage:

- **Streaming progress** — step transitions, character counts per author
- **Tool execution** — path resolution, file sizes, chunk parsing details
- **Token usage** — per-agent input/output/total tokens, cached tokens, extra usage fields
- **GitHub operations** — repo connection, file SHA on updates, commit URLs

---

## Best Practices Applied

The InfraPrepAgent applies these Azure Bicep best practices:

| Rule | Description |
|---|---|
| **Tags** | All resources get `Environment`, `Project`, `ManagedBy`, `LastDeployed` tags |
| **Diagnostics** | Diagnostic settings enabled for supported resources (logs to Log Analytics) |
| **Naming** | Cloud Adoption Framework (CAF) pattern: `{prefix}-{workload}-{env}` |
| **Comments** | Section comments added to each resource block |
| **Secure params** | `@secure()` decorator on sensitive parameters |
| **Storage** | `allowBlobPublicAccess: false`, `minimumTlsVersion: 'TLS1_2'` |
| **Key Vault** | `enableRbacAuthorization: true`, `enablePurgeProtection: true` |
