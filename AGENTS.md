# Repository guide

## 1. What this project is
- Streamlit admin app for Layla catering/menu operations; generates printable PDFs instead of serving end-user web pages.
- Core user flow: maintain a dish catalog with EN/AR names, macros, and dietary flags, then export branded print assets.
- Main outputs: A4 dish cards, Easter greeting labels, buffet menu PDFs, and delivery-note PDFs.
- Data can run locally from `data/dishes.csv` or persist to Firestore; the UI prefers Firestore when credentials exist.
- AI is optional and used only for admin assistive workflows: single-dish autofill, Arabic translation, and batch idea/recipe draft generation.

## 2. Tech stack and runtime
- Python 3 app; no package/build system beyond `requirements.txt`.
- UI/runtime: Streamlit (`streamlit run app.py`).
- Data/modeling: pandas DataFrames + dataclasses; no ORM.
- PDF/rendering: ReportLab; optional Arabic shaping via `arabic-reshaper` + `python-bidi`.
- Optional services: Firebase Admin SDK + Firestore + Firebase Auth REST API; OpenAI-compatible and Gemini-compatible JSON completions over `urllib`.
- Optional preview/runtime extras: PyMuPDF (`pymupdf`) for layout preview in the Layout Tuner.

## 3. Repo map
- `app.py`: single Streamlit entrypoint; auth gate, Firestore init, workspace routing, UI actions.
- `cards.py`: rendering engine for dish cards, greeting labels, buffet menus, delivery notes, layout config, font/icon/template handling.
  - Delivery note rendering now uses a fixed-grid `DeliveryNoteLayoutSpec`; update the layout spec and helpers together instead of nudging isolated coordinates.
- `ai_client.py`: provider-agnostic JSON completion client; reads env/secrets and calls Gemini/OpenAI-style endpoints.
- `translation.py`: glossary/existing-data-aware Arabic translation, optionally AI-backed.
- `enrich.py`: single-dish AI autofill for macros/flags + translation.
- `ai_recipe_studio.py`: structured multi-draft generation, validation, evaluation, storage, approve/reject lifecycle.
- `firebase_auth_service.py`: Firebase email/password sign-in + token refresh helpers.
- `idea_center.py`: curated preset prompts for AI Recipe Studio.
- `data/`: source CSV and layout JSON profiles; also local JSON draft storage.
- `assets/`: logo, icons, fonts, optional full-page background template.
- `tools/`: Firestore seeding script and DOCX layout extraction utility.
- `tests/`: `unittest` suite for rendering helpers, translation, auth helpers, and AI draft lifecycle.
- `out/`: generated artifacts/debug exports; not source of truth.

## 4. Execution flow
- Entry point: `app.py` sets Streamlit config, initializes Firestore once, then enforces `_render_login_gate()` before loading the dish DB.
- Data load path: `_load_dishes()` -> Firestore if init succeeds, otherwise local CSV; if Firestore is empty and auto-seed is enabled, CSV is pushed into Firestore.
- UI is a single segmented-control workspace in `app.py`; each workspace directly triggers helper functions rather than calling a separate service layer.
- Dish rendering path: selected DataFrame rows -> `Dish` dataclasses -> `cards.generate_cards_pdf()` / `generate_greeting_labels_pdf()` / `generate_buffet_menu_pdf()` / `generate_delivery_note_pdf()` -> file under `out/` -> Streamlit download button.
- Dish CRUD path: `st.data_editor` / add-dish review table -> `_save_dishes()` or `_save_candidate_rows()` -> full CSV rewrite or Firestore batch sync/upsert.
- AI autofill path: `enrich.enrich_dish_name()` -> `ai_client.request_json_completion()` -> `translation.translate_dish_name()` -> editable candidate rows.
- AI Recipe Studio path: `GenerationRequest` -> `generate_dish_drafts()` -> validate/repair/evaluate/translate -> save drafts to Firestore or `data/generated_dish_drafts.json` -> approve into main dish store or reject/supersede.
- Business logic mostly lives in `app.py`, `cards.py`, `ai_recipe_studio.py`, and `translation.py`; there is no deeper application/service package split.

## 5. Data and contracts
- No relational DB, ORM, or migrations. Persistent app data is either Firestore documents or local files.
- Canonical dish schema is fixed in multiple places: `name_en`, `name_ar`, `calories_kcal`, `carbs_g`, `protein_g`, `fat_g`, `gluten`, `protein_type`, `dairy`.
- Dish records are normalized aggressively; missing flags default to `gluten_free`, `veg`, `dairy_free`, numeric fields coerce to `0.0`.
- Firestore dish collection defaults to `dishes`; doc IDs are slugified from `name_en` and deduplicated with numeric suffixes.
- Draft storage schema is the `GeneratedDishDraft` dataclass in `ai_recipe_studio.py`; stored either as Firestore docs or a JSON list on disk.
- Internal boundaries:
  - `app.py` owns UI state, backend selection, and save semantics.
  - `cards.py` owns visual layout/rendering only.
  - `translation.py` and `enrich.py` own AI-assisted field generation.
  - `firebase_auth_service.py` owns Firebase Auth REST payloads/session parsing.
- External API contracts:
  - Firebase Auth sign-in: `accounts:signInWithPassword`.
  - Firebase token refresh: `securetoken.googleapis.com/v1/token`.
  - OpenAI path prefers `/v1/responses`, falls back to `/v1/chat/completions`, always expecting a JSON object response.
  - Gemini path uses the native `models/*:generateContent` API with `generationConfig.responseMimeType=application/json`.

## 6. Config and environment
- Firebase auth:
  - `FIREBASE_WEB_API_KEY`
  - `FIREBASE_AUTH_REQUIRED` default `true`
- Firebase admin / Firestore:
  - `FIREBASE_SERVICE_ACCOUNT_JSON` or `FIREBASE_SERVICE_ACCOUNT_PATH`
  - `FIREBASE_PROJECT_ID`
  - `FIREBASE_DISHES_COLLECTION` default `dishes`
  - `FIREBASE_DRAFTS_COLLECTION` default `generated_dish_drafts`
  - `FIREBASE_AUTO_SEED_FROM_CSV` default `true`
  - `GOOGLE_APPLICATION_CREDENTIALS` fallback for admin creds
- AI providers:
  - `GEMINI_API_KEY` or `GOOGLE_API_KEY`, `GEMINI_MODEL`, optional `GEMINI_BASE_URL`
  - `OPENAI_API_KEY`, `OPENAI_MODEL`, optional `OPENAI_BASE_URL`
  - `AI_TRANSLATION_PROVIDER`
  - `GEMINI_TRANSLATION_MODEL`
  - `OPENAI_TRANSLATION_MODEL`
- UI/dev flags:
  - `SHOW_LAYOUT_TUNER` default `true`
- Config gotchas:
  - `ai_client.py` reads both process env and Streamlit secrets; secrets are not auto-exported to env.
  - If a local `*firebase-adminsdk*.json` or `*service-account*.json` file exists in repo root, app/scripts may auto-detect it.
  - No feature-flag framework; booleans are ad hoc env/secrets checks.

## 7. Developer workflow
- Install: `python3 -m pip install -r requirements.txt`
- Dev app: `streamlit run app.py`
- Tests: `python3 -m unittest discover -s tests`
- Lint: no linter config committed.
- Build/package: none.
- Typecheck: none.
- Firestore seed:
  - `python3 tools/seed_firestore.py --csv data/dishes.csv --collection dishes`
  - add `--replace` only when intentionally deleting remote docs missing from CSV
- DOCX layout extraction:
  - `python3 tools/dump_docx_layout.py --docx /abs/path/template.docx --out out/docx_layout.json`
- Local troubleshooting:
  - If Arabic renders as boxes, add a `.ttf` under `assets/fonts/`.
  - If layout preview fails, ensure `pymupdf` is installed.
  - If app falls back to CSV unexpectedly, inspect Firestore credential env/secrets and `_FIRESTORE_INIT_ERROR` in the UI.

## 8. Code conventions
- Simple module-level architecture; prefer extending existing helper functions over introducing new abstraction layers unless the app is being structurally refactored.
- Use existing dataclasses (`Dish`, `LayoutConfig`, `GeneratedDishDraft`, `FirebaseAuthSession`) for typed boundaries.
- Keep dish schema synchronized everywhere it is duplicated: normalization helpers, save helpers, AI draft conversion, tests, CSV/Firestore utilities.
- Preserve UTF-8 with BOM handling for CSV reads/writes (`utf-8-sig`), or headers can drift.
- Streamlit state is stored in `st.session_state`; changes that affect generated downloads often also need session-state invalidation.
- Rendering code is coordinate-sensitive; small layout edits can visually regress print output.
- Avoid assuming Firestore and CSV backends behave identically: full saves replace the whole dataset, but single upserts use different paths.

## 9. Change guide
- UI change:
  - Edit workspace logic in `app.py`.
  - Edit PDF visuals/layouts in `cards.py`.
  - The `Buffet A4 Menu` workspace now owns both buffet-menu and delivery-note generation.
- API change:
  - AI provider request/response handling in `ai_client.py`.
  - Firebase Auth REST handling in `firebase_auth_service.py`.
- DB change:
  - Dish schema/load-save logic in `app.py` and `tools/seed_firestore.py`.
  - Draft schema/storage in `ai_recipe_studio.py`.
  - No migrations system exists; data compatibility is manual.
- Auth change:
  - Login/session behavior in `app.py` + `firebase_auth_service.py`.
  - Server-side token verification is done in `app.py` using Firebase Admin SDK.
- Background job change:
  - None present; all AI generation and rendering run inline during Streamlit interactions.
- Integration change:
  - Translation/autofill/model selection in `translation.py`, `enrich.py`, `ai_recipe_studio.py`, and `ai_client.py`.
  - Firestore import/export in `app.py` and `tools/seed_firestore.py`.

## 10. Risky areas
- `app.py` is the control center and mixes UI, persistence, and orchestration; regressions here can break multiple workspaces at once.
- `_save_dishes()` with Firestore backend performs replace-style sync; deleting a row in the editor deletes the remote doc on save.
- `_seed_firestore_from_csv_if_empty()` writes CSV contents to Firestore automatically; be careful when testing against an empty production-like project.
- Auth model is authentication-only; there is no repo-defined role/claim authorization beyond “signed in or bypassed”.
- AI draft approval writes into the main dish store; schema drift or validation mistakes affect production dish data.
- Rendering changes in `cards.py` can break print alignment, pagination, template background behavior, or Arabic text fitting.
- Delivery-note changes are especially sensitive because header/table/footer geometry is coupled through the shared layout spec; avoid ad hoc offsets outside that spec.
- Service account auto-detection from repo root can cause the app/script to bind to a local project unintentionally.

## 11. Definition of done
- Run `python3 -m unittest discover -s tests`.
- If rendering/layout changed, generate the relevant PDF locally and visually inspect the affected output.
- If schema/persistence changed, verify both CSV fallback and Firestore-backed paths still normalize/save correctly.
- If AI-related code changed, verify behavior with AI unconfigured as well as configured fallback logic where relevant.
- Update this file when the task reveals a durable repo-specific fact worth preserving.

## 12. Maintenance rule
- Whenever you learn something repo-specific that will help future tasks, update this file in the same change.

## 13. Open questions / unknowns
- No deployment config is committed; target runtime beyond local Streamlit usage is not encoded in the repo.
- No explicit production/preview environment separation is defined for Firestore projects or collections.
- No committed lint/typecheck standard exists, so only the test suite defines an enforced check today.
