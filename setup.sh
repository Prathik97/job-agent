#!/usr/bin/env bash
# Run this once from inside the job-agent folder:  bash setup.sh
set -e

echo "Creating folders..."
mkdir -p scout screener tailor tracker data/raw data/clean output/resumes tests

echo "Creating .gitignore..."
cat > .gitignore << 'EOF'
.env
.venv/
__pycache__/
data/
output/
*.pyc
.DS_Store
credentials.json
EOF

echo "Creating .env.example..."
cat > .env.example << 'EOF'
# Copy this file to .env and fill in real values. Never commit .env.
ANTHROPIC_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
GOOGLE_SHEET_ID=
FIRECRAWL_API_KEY=
ADZUNA_APP_ID=
ADZUNA_APP_KEY=
RAPIDAPI_KEY=
EOF

echo "Creating requirements.txt..."
cat > requirements.txt << 'EOF'
anthropic
python-dotenv
pyyaml
pandas
requests
gspread
google-auth
python-telegram-bot
pydantic
EOF

echo "Creating Python virtual environment..."
python3 -m venv .venv

echo ""
echo "Done. Next, run these two commands:"
echo "  source .venv/bin/activate        (Windows: .venv\\Scripts\\activate)"
echo "  pip install -r requirements.txt"
