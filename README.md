# AI Wiki PoC v1.1

[![CI](/actions/workflows/ci.yml/badge.svg)](/actions/workflows/ci.yml)

## Current Runtime

The project automatically loads a local `.env` through `python-dotenv` when
the backend starts. SQLite LLM cache, SQLite vector storage, semantic search,
and grounded RAG chat are implemented. The existing ChatGPT Site frontend is
kept at `https://ai-wiki-dashboard-minwoo.cheatmin.chatgpt.site`.

시간에 따라 들어오는 Source 중 처음 보는 content만 Gemini에 전달하고, 구조화된 분석 결과를 Python이 결정론적인 Markdown Wiki로 렌더링하는 최소 PoC입니다.

## 설계 경계

- `collectors/mock_collector.py`: 네트워크 없는 테스트용 가상 데이터를 반환합니다.
- `collectors/real_collector.py`: OpenAI 공식 News RSS, Google 공식 Gemini Blog RSS, Anthropic Newsroom에서 최신 공개 항목을 공급자별로 수집합니다.
- `engine/schemas.py`: `SourceRecord`와 `WikiUpdatePayload` 계약을 분리합니다.
- `engine/wiki_poc.py`: SHA-256 Delta Gate, Semantic Validation, 상태 병합, Markdown 렌더링을 담당합니다.
- `wiki/`: 생성된 Markdown만 저장합니다. `seen_hashes`와 병합 상태는 Markdown Frontmatter에 있습니다.
- LLM은 판단과 요약만 수행합니다. ID 검증, 정렬, 최근 변경 5개 제한, History append는 Python이 수행합니다.

SQLite, Vector DB, RAG, Claim Verification, 실제 웹 크롤링은 v1.0 범위에 포함하지 않습니다.

## 실행

Python 3.10 이상을 권장합니다.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

`.env.example`을 참고해 환경변수를 셸이나 CI에 주입합니다. 이 PoC는 `.env`를 직접 읽지 않으며 실제 API Key를 커밋하면 안 됩니다.

```bash
export GEMINI_API_KEY="..."
export GEMINI_MODEL="gemini-3.1-flash-lite"
python -m engine.real_wiki
```

각 collector는 독립적으로 실패를 처리하므로 한 공식 소스의 네트워크 장애가 다른 소스 수집을 막지 않습니다. 결과는 `wiki/openai.md`, `wiki/gemini.md`, `wiki/claude.md`에 각각 저장됩니다. 동일 Source를 다시 실행하면 `seen_hashes`에서 감지되어 Gemini API를 호출하지 않습니다.

## 테스트

```bash
pytest -q
```

테스트는 Fake Analyzer를 사용해 API Key나 네트워크 없이 신규 Markdown 생성, Delta Gate, 잘못된 Source ID 제거, Recent Changes 최신 5개 제한을 검증합니다.

GitHub Actions는 매일 00:17 UTC(09:17 KST)에 실행되며 수동 실행도 가능합니다. 저장소 Secret `GEMINI_API_KEY`를 등록해야 합니다.

## Obsidian Vault import

Vault는 읽기 전용 Source Provider로 처리됩니다. `.obsidian`, `.trash`, `attachments` 및 hidden directory를 제외한 모든 Markdown을 재귀적으로 읽습니다.

```powershell
$env:OBSIDIAN_VAULT_PATH="C:\Users\your-name\Documents\MyVault"
python -m engine.obsidian_import
```

For a full Vault import on the Gemini free tier, the recommended settings are:

```dotenv
GEMINI_MODEL=gemini-3.1-flash-lite
OBSIDIAN_IMPORT_DELAY_SECONDS=5
OBSIDIAN_IMPORT_MAX_RETRIES=3
```

The same settings can be overridden for one run with `--delay-seconds` and
`--max-retries`. Unchanged documents are skipped without a Gemini call. HTTP
429 responses use the provider retry delay when available, and HTTP 503
responses retry after 30, 60, and 120 seconds.

상태는 Vault가 아니라 프로젝트 루트의 `.obsidian_import_state.json`에 저장됩니다. 동일 relative path의 content hash가 그대로이면 Gemini를 호출하지 않고, 내용이 바뀌면 다시 분류합니다. `AI`, `투자`, `철학` 같은 폴더명은 category hint로만 전달되며 최종 분류는 Gemini Structured Output과 Pydantic validation으로 결정됩니다.

## Personal knowledge inbox

`inbox/`에 다음 형식의 Markdown 파일을 추가하면 Gemini가 `ai`, `investment`, `philosophy`, `misc` 중 하나를 primary category로 선택하고 `wiki/<category>/index.md`에 반영합니다.

```markdown
---
type: article
source_url: https://example.com/article
title: Optional title
---

기사 내용 또는 저장한 텍스트
```

```bash
python -m engine.inbox_wiki
```

## Dashboard

Run the FastAPI server and the React dashboard in separate terminals:

```powershell
uvicorn app:app --reload
cd frontend
npm install
npm run dev
```

The dashboard uses `http://localhost:8000` by default. Set
`VITE_API_BASE_URL` in `frontend/.env.local` when the API runs elsewhere.

## Semantic search

Semantic indexing uses the local Ollama embedding API. Install and run the
selected embedding model before importing:

```powershell
ollama pull bge-m3
```

`POST /import` synchronizes changed Markdown files into `vectors.db` and
removes vectors for deleted files. Query semantic matches with
`GET /semantic-search?q=...`. Set `EMBEDDING_MODEL` to `nomic-embed-text` if
that model is preferred.

The API also exposes `POST /chat` with a JSON body such as
`{"question":"What does the vault say about retrieval?"}`. It retrieves the
five closest indexed documents, builds a grounded context prompt, and returns
the answer with source IDs and similarity scores. The Dashboard's RAG Chat
panel opens those sources in the Markdown preview.

## News Collector Agent

The News Agent collects official OpenAI, Google AI, Anthropic, and Ollama
updates every 30 minutes. New entries are written as Markdown into the
Obsidian vault's `Inbox/` directory, where the existing Watchdog importer can
process them.

Run one collection cycle:

```powershell
python -m engine.news_agent --once
```

Run continuously:

```powershell
python -m engine.news_agent
```

Seen URLs are stored in `news_agent.db`, so the same article is never written
twice. Feed failures are retried with exponential backoff and do not stop the
other sources.

## Unified import pipeline

All single-file imports use the same pipeline: `import_file()` updates the
Wiki, cache, and state first, then `update_vector()` synchronizes embeddings
and removes vectors for deleted documents. Watchdog calls `run_pipeline()` for
changed files, and `POST /import` runs the same pipeline for each discovered
Markdown file.

## Docker

Copy `.env.example` to `.env` when local secrets or settings need to be
customized, then start the complete stack:

```powershell
docker compose up --build
```

The backend is available at `http://localhost:8000`, the dashboard at
`http://localhost:5173`, and Ollama at `http://localhost:11434`. Wiki, cache,
state, and Ollama model data are persisted in `wiki/`, `cache/`, `state/`, and
`ollama/` respectively. Health checks are enabled for all application
services.

## MCP Server

The MCP server exposes the existing FastAPI API over stdio. Start the API
first, then configure an MCP client with the following command:

```json
{
  "mcpServers": {
    "ai-wiki": {
      "command": "C:\\Users\\your-name\\Desktop\\My project\\ai-wiki-poc\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server.server"],
      "env": {"AI_WIKI_API_URL": "http://localhost:8000"}
    }
  }
}
```

The same stdio configuration can be registered in Claude Desktop, Cursor, or
another MCP-compatible client. Available tools are `search_notes`,
`ask_wiki`, `recent_notes`, `stats`, and `import_now`.

## External API Deployment

Deploy the backend as an HTTPS service using the root `Dockerfile` and expose
port `8000`. The ChatGPT Site is already configured as the default CORS origin:

```text
https://ai-wiki-dashboard-minwoo.cheatmin.chatgpt.site
```

Additional origins can be supplied with the comma-separated
`CORS_ALLOW_ORIGINS` environment variable. Set `GEMINI_API_KEY` only as a
server-side secret in the deployment provider; never commit it to `.env` or
the image. The `.env` file is loaded automatically for local runs, while
production values should be injected by the hosting platform.

The `/health` endpoint is safe for deployment probes. `/stats`, `/recent`,
`/search`, and `/note/{id}` are cloud-safe read APIs backed by committed Wiki
Markdown. `/import` is local-only and is disabled by default; enable it with
`ENABLE_LOCAL_IMPORT=true` only when the backend can access the Obsidian Vault.
`/semantic-search` and `/chat` are also local-only by default and require
`ENABLE_LOCAL_RAG=true`, Ollama, and the SQLite vector DB.

The deployment split is:

```text
Cloud:
ChatGPT Site
  -> HTTPS FastAPI
  -> committed wiki Markdown

Local:
Obsidian
  -> Watchdog / Pipeline
  -> Wiki
  -> Vector DB / Ollama
  -> GitHub push
```

For local development, use:

```dotenv
ENABLE_LOCAL_IMPORT=true
ENABLE_LOCAL_RAG=true
```

### Render Deployment

The repository includes a Render Blueprint in `render.yaml`. To deploy the
cloud read API:

1. Connect the GitHub repository to Render.
2. Create a Blueprint using `render.yaml` and deploy the `main` branch.
3. Add `GEMINI_API_KEY` as a Render secret; do not put its value in
   `render.yaml` or commit it to the repository.
4. After deployment, verify `https://<render-service>.onrender.com/health`.
5. Use the generated HTTPS base URL as the API base URL for the existing
   ChatGPT Site frontend.

Render keeps `ENABLE_LOCAL_IMPORT=false` and `ENABLE_LOCAL_RAG=false`, so it
does not require an Obsidian Vault, Ollama, or the local vector database.
Only the committed Wiki Markdown read APIs are enabled in the cloud.

성공한 자료의 URL identity, content hash, 입력 파일명, 처리 시각은 `inbox/.processed.json`에 기록됩니다. 동일 URL 또는 동일 content는 Gemini를 다시 호출하지 않으며, 실패한 자료는 processed 상태로 기록되지 않습니다. 내부 source ID와 content hash는 category Wiki 본문에 렌더링하지 않습니다.
