# Layla Cards Generator (MVP)

This project generates **printable A4 PDF cards** (2 columns × 3 rows) like your manual paper.

## What you get
- Enter / store dish info once (EN + AR + macros + nutriment flags)
- One click → **PDF ready to print**
- Icons + logo per card

## Folder structure
- `data/dishes.csv` — your dish database (edit/add dishes here)
- `assets/logo.png` — your logo
- `assets/icons/*.png` — icons (gluten / gluten_free / veg / meat / dairy / dairy_free)
- `out/` — generated PDFs

## 1) Install
```bash
pip install streamlit pandas reportlab pillow certifi
# Optional but recommended for correct Arabic rendering:
pip install arabic-reshaper python-bidi
```

## 2) Run
```bash
streamlit run app.py
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

---
If you have your exact **blank template background** (the empty page with only the logo), we can also switch to a “background image” mode so the PDF matches your paper pixel-perfect.
