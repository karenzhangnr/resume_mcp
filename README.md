# Resume Updater MCP Server

A local MCP (Model Context Protocol) server that lets Claude tailor your LaTeX resume for specific job applications — selecting curated bullets, rewriting the title and summary, and compiling a PDF.

---

## How It Works

When registered, Claude Code gains 5 tools it can call during a conversation:

| Tool | What it does |
|------|-------------|
| `classify_jd` | Reads a job description and scores it as DS / DA / AI / FDE / DE |
| `get_bullets` | Returns your curated bullet library filtered by job type |
| `add_bullet` | Adds a new bullet to the database |
| `update_resume` | Writes a new versioned `.tex` file with your chosen bullets, title, and summary |
| `compile_pdf` | Runs pdflatex and opens the finished PDF |

### Conversation workflow

```
1. Paste a job description
2. Claude calls classify_jd → confirms job type (DS / DA / AI / FDE / DE)
3. Claude calls get_bullets → presents all available bullets for your review
4. You pick 8 Company A + 4 Company B (or swap any)
5. Claude drafts a 50-60 word summary → you review and approve / edit
6. Claude calls update_resume → saves resume_ds_MMDD_XX.tex
7. Claude calls compile_pdf → PDF opens automatically
```

`resume_ds.tex` is **never modified** — every application gets its own dated file.

---

## Stellantis Title Logic

The `update_resume` tool automatically sets the Stellantis job title based on job type:

| Job type | Title written to .tex |
|---|---|
| DS | `Data Scientist ` |
| DA | `Business Analytics` |
| AI / FDE | `AI Engineer` |
| DE | `AI Engineer` |
| Any + LLM/GenAI in JD | appends `\| AI Engineer` |

---

## Directory Structure

```
Projects/Claude/
├── .mcp.json                   ← MCP server registration (project-scope)
├── resume/
│   └── resume.tex           ← Master template (read-only)
│   └── resume_MMDD_XX.tex   ← Generated output files
│   └── resume_MMDD_XX.pdf
└── mcp_server/
    ├── server.py               ← FastMCP server (5 tools)
    ├── bullets_db.json         ← Curated bullet library
    ├── requirements.txt
    └── README.md               ← You are here
```

---

## Setup

### 1. Install dependencies

```powershell
pip install -r mcp_server/requirements.txt
```

### 2. Verify MiKTeX is installed

The server uses pdflatex at:
```
C:\Users\...\AppData\Local\Programs\MiKTeX\miktex\bin\x64\pdflatex.exe
```

### 3. Register with Claude Code

The `.mcp.json` at the project root handles registration automatically. After any changes, restart Claude Code (VS Code) to reload.

To verify the server is registered:
```powershell
claude mcp list
```

You should see `resume-updater` in the output.

---

## Bullet Database (`bullets_db.json`)

The database stores a library of resume bullets. Each job has more bullets than required (currently 21 for Stellantis, 4 for Santander) so you can pick the best fit for each application.

**Required counts per application:**
- Company A: **8 bullets**
- Company B: **4 bullets**

### Adding bullets via conversation

Ask Claude in any conversation:
> "Add this bullet to Company A: `<bullet text>`"

Claude will call `add_bullet` and save it to the database.

### Bullet format

Bullets are stored as plain strings. Use LaTeX `\textbf{}` for bold emphasis when you want it to appear bold in the PDF:

```
\textbf{Built and executed} validation testing for credit risk scoring models...
```

---

## Output File Naming

Files are saved as:
```
resume_MMDD_XX.tex / .pdf
```
- `MMDD` = today's date (e.g. `0520` for May 20)
- `XX` = first 2 letters of company name (e.g. `Ly` for Lyft, `Go` for Google)

---

## Tested With

- Python 3.14
- MCP SDK (`mcp[cli]`)
- MiKTeX (pdflatex)
- Claude Code (VS Code extension)
