# AI Wiki PoC v1.0

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
export GEMINI_MODEL="gemini-3.7-flash"
python -m engine.real_wiki
```

각 collector는 독립적으로 실패를 처리하므로 한 공식 소스의 네트워크 장애가 다른 소스 수집을 막지 않습니다. 결과는 `wiki/openai.md`, `wiki/gemini.md`, `wiki/claude.md`에 각각 저장됩니다. 동일 Source를 다시 실행하면 `seen_hashes`에서 감지되어 Gemini API를 호출하지 않습니다.

## 테스트

```bash
pytest -q
```

테스트는 Fake Analyzer를 사용해 API Key나 네트워크 없이 신규 Markdown 생성, Delta Gate, 잘못된 Source ID 제거, Recent Changes 최신 5개 제한을 검증합니다.

GitHub Actions는 매일 00:17 UTC(09:17 KST)에 실행되며 수동 실행도 가능합니다. 저장소 Secret `GEMINI_API_KEY`를 등록해야 합니다.
