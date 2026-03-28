# Layla Cards Generator (MVP)

This project generates **printable A4 PDF cards** (2 columns × 3 rows) like your manual paper.

## What you get
- Enter / store dish info once (EN + AR + macros + nutriment flags)
- One click → **PDF ready to print**
- Icons + logo per card

## Folder structure
- `data/dishes.csv` — local fallback dish database (used if Firebase is not configured)
- `assets/logo.png` — your logo
- `assets/icons/*.png` — icons (gluten / gluten_free / veg / meat / dairy / dairy_free)
- `out/` — generated PDFs

## 1) Install
```bash
pip install streamlit pandas reportlab pillow certifi firebase-admin
# Optional but recommended for correct Arabic rendering:
pip install arabic-reshaper python-bidi
```

## 2) Run
```bash
streamlit run app.py
```

## 2.1) Firebase persistence (recommended)
To persist dishes across Streamlit sleep/restart, configure Firestore credentials.

Environment variables or Streamlit secrets:
- `FIREBASE_SERVICE_ACCOUNT_JSON` (service account JSON as a string) OR
- `FIREBASE_SERVICE_ACCOUNT_PATH` (path to service account JSON file)
- Optional: `FIREBASE_PROJECT_ID`
- Optional: `FIREBASE_DISHES_COLLECTION` (default: `dishes`)
- Optional: `FIREBASE_AUTO_SEED_FROM_CSV` (default: `true`)

Example `.streamlit/secrets.toml`:
```toml
FIREBASE_SERVICE_ACCOUNT_JSON='{"type":"service_account","project_id":"...","private_key_id":"...","private_key":"-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n","client_email":"...","client_id":"...","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token","auth_provider_x509_cert_url":"https://www.googleapis.com/oauth2/v1/certs","client_x509_cert_url":"..."}'
FIREBASE_PROJECT_ID="your-project-id"
FIREBASE_DISHES_COLLECTION="dishes"
```

If Firebase is not configured, the app falls back to `data/dishes.csv`.

### Seeding behavior
- Automatic bootstrap: when Firestore is enabled and empty, the app auto-seeds from `data/dishes.csv` once.
- Manual script (recommended for controlled reseed): use `tools/seed_firestore.py`.

Examples:
```bash
# Safe upsert from CSV (no deletes)
python tools/seed_firestore.py --csv data/dishes.csv --collection dishes

# Full replace (Firestore mirrors CSV exactly)
python tools/seed_firestore.py --csv data/dishes.csv --collection dishes --replace
```

## 3) Arabic rendering (important)
ReportLab needs:
1) Arabic shaping (`arabic-reshaper` + `python-bidi`)
2) A font that supports Arabic glyphs (TTF), e.g.:
- Noto Naskh Arabic
- Amiri

Put the font file in:
`assets/fonts/` (any `.ttf` file)

The app will auto-pick the first `.ttf` it finds in `assets/fonts/`.

## 3.1) Base template page (optional, recommended)
If your print template already includes borders/logo, add:
- `assets/template_page.png`

When this file exists, the generator uses it as full-page background and does not draw card grid lines/logo on top.

## 4) Dish database schema
`data/dishes.csv` columns:
- `name_en`
- `name_ar`
- `calories_kcal`
- `carbs_g`
- `protein_g`
- `fat_g`
- `gluten` = `gluten` or `gluten_free`
- `protein_type` = `veg` or `meat`
- `dairy` = `dairy` or `dairy_free`

## 5) Translation + “fetch macros”
### Recommended approach (reliable)
- Store the Arabic name + macros **once** per dish in the CSV.
- “Auto-translate” is only used to propose a draft for new dishes.

Why: for real kitchen portions, **generic nutrition APIs won’t match your recipe/portion size** unless you model ingredients & serving sizes.

### Phase 2 (if you want automatic macro calculation)
- Add a recipe table: ingredients + grams + yield/servings
- Use a food database (USDA, OpenFoodFacts, etc.) for ingredient nutrients
- Compute macros per serving automatically

If you want, I can extend this MVP to include **recipe-based macro calculation** and **barcode/ingredient lookup**.

## 6) Optional auto-fill (AI)
There is an **Add Dish (Auto-fill)** tab that can propose Arabic + macros + flags.

### Recommended: OpenRouter
Set these environment variables before running Streamlit:
- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL`

Optional (recommended by OpenRouter):
- `OPENROUTER_SITE_URL`
- `OPENROUTER_APP_NAME`

Optional:
- `OPENROUTER_BASE_URL` (defaults to `https://openrouter.ai/api/v1`)

#### Streamlit secrets (easy local setup)
Create `/Users/mohamadsafar/Documents/layla_cards_system/.streamlit/secrets.toml`:
```toml
OPENROUTER_API_KEY="your_key_here"
OPENROUTER_MODEL="your_model_here"
```

### Alternative: OpenAI
Set:
- `OPENAI_API_KEY`
- `OPENAI_MODEL`

Optional (for OpenAI-compatible endpoints):
- `OPENAI_BASE_URL` (defaults to `https://api.openai.com`)

## 7) Template alignment workflow (DOCX -> JSON -> debug overlay)
Use the script below to dump Word layout coordinates (shapes/text/images):

```bash
python tools/dump_docx_layout.py \
  --docx "/absolute/path/to/template.docx" \
  --out "out/docx_layout.json"
```

Then generate a debug PDF by passing `DebugOverlayOptions` to `generate_cards_pdf(...)` in `cards.py`:
- `show_grid=True` for card/page guides
- `show_docx_shapes=True` + `docx_layout_json=...` for DOCX shape boxes
- `reference_image=...` (optional PNG/JPG page export from your template PDF)

## 8) No-code layout tuning in app
Use the **Layout Tuner** tab in Streamlit to adjust coordinates/sizes without editing code.
- Values are saved to `data/layout_config.json`
- `Generate PDF` automatically uses this config
- Use small increments (0.1 mm) and iterate

## 9) Two layout profiles
In **Generate PDF**, you can select:
- `Full (with macros)` -> uses `data/layout_config.json`
- `Names + Nutriments (no macros)` -> uses `data/layout_no_macros.json`

The no-macros profile centers names + nutriment icons and uses slightly larger font/icon defaults.

---
If you have your exact **blank template background** (the empty page with only the logo), we can also switch to a “background image” mode so the PDF matches your paper pixel-perfect.
