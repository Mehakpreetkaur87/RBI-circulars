# Reserve Bank of India (RBI) Circulars RAG & Evaluation Pipeline

An end-to-end **Retrieval-Augmented Generation (RAG)** and **Automated Evaluation System** designed for complex regulatory compliance circulars issued by the **Reserve Bank of India (RBI)**.

The system downloads and cleans official RBI notifications, creates semantic vector embeddings and keyword search indices, synthesizes cited answers using **Google Gemini**, and evaluates factual consistency and completeness using **DeepSeek** via **AgentRouter** and the **RAGAS** framework.

---

## 🌟 Key Highlights

* **Automated Ingestion**: Crawls RBI circulars, downloads PDFs, and extracts tables & text using HTML-first parsing, PDF fallbacks (`PyMuPDF`, `pdfplumber`), and OCR fallback.
* **Token-Aware Chunking**: Slices circulars into 512-token segments (100-token overlap) preserving sentence and table integrity without cutting words.
* **Hybrid Search Retrieval**: Combines dense semantic vector search (`BAAI/bge-m3` + FAISS) with sparse lexical keyword matching (BM25 Okapi).
* **Gemini Answer Generation**: Produces answers cited with circular numbers and paragraphs, backed by multi-key failover and per-question atomic checkpoints.
* **Rigorous LLM-as-a-Judge Evaluation**: Scores answers against gold-standard benchmarks across 4 RAGAS metrics (Faithfulness, Answer Relevancy, Context Precision, Context Recall), exporting a 4-sheet Excel report.

---

## 📁 Repository Structure

```text
RBI_circulars_project/
├── .env.example                       # Template for API keys and configuration
├── .gitignore                         # Configured rules for secrets, venv, and large files
├── README.md                          # Main project documentation & setup guide
├── requirements.txt                   # Pinned Python package dependencies
├── version1.md                        # Simple, step-by-step pipeline execution guide
└── V1/                                # Version 1 implementation
    ├── ingest_one_circular_final_v2.py # Step 1: Crawl & extract circulars
    ├── chunk_embed_16_09_v3.py        # Step 2: Token chunking & FAISS/BM25 indexing
    ├── gemini_rbi_answer_generation.py# Step 3: Top-K retrieval & Gemini answer generation
    ├── agent_router_eval_v2.py        # Step 4: AI judge evaluation & Excel reporting
    ├── test_file.json                 # Curated 400-question benchmark dataset
    ├── agentrouter_answer_eval_v2.json # Sample evaluation results (JSON)
    └── agentrouter_answer_eval_v2.xlsx # Sample evaluation results (4-sheet Excel workbook)
```

---

## 🛠️ Getting Started & Setup

Follow these simple steps to set up and run the project locally.

### 1. Prerequisites
* **Python**: Version `3.10`, `3.11`, or `3.12` installed.
* **Git**: Installed on your system.
* **API Keys**:
  * **Google Gemini API Key**: For answer generation ([Get one at Google AI Studio](https://aistudio.google.com/app/apikey)).
  * **AgentRouter API Key**: For DeepSeek evaluation judge ([Get one at AgentRouter](https://agentrouter.org)).

---

### 2. Clone the Repository

```bash
git clone https://github.com/Mehakpreetkaur87/RBI-circulars.git
cd RBI-circulars
```

---

### 3. Create and Activate a Virtual Environment

It is strongly recommended to use a virtual environment to prevent package conflicts:

#### On Windows (PowerShell):
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

*(If PowerShell displays an execution policy warning, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first)*

#### On Windows (Command Prompt):
```cmd
python -m venv venv
venv\Scripts\activate.bat
```

#### On macOS / Linux:
```bash
python3 -m venv venv
source venv/bin/activate
```

---

### 4. Install Dependencies

Install all required Python packages with a single command:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

---

### 5. Configure API Keys (`.env`)

Create your `.env` file by copying the provided `.env.example`:

#### On Windows:
```powershell
copy .env.example .env
```

#### On macOS / Linux:
```bash
cp .env.example .env
```

Open `.env` in any text editor and fill in your actual API keys:

```env
# Google Gemini API Key (Required for Step 3)
GEMINI_API_KEY=your_actual_gemini_key_here

# AgentRouter API Key (Required for Step 4)
AGENT_ROUTER_API=your_actual_agentrouter_key_here

# Optional: Limit questions during generation testing (default: 30)
RAG_MAX_QUESTIONS=30
```

> ⚠️ **Security Notice**: Never commit your `.env` file to git. It is included in `.gitignore` by default.

---

## 🏃 Running the Pipeline

For a detailed, beginner-friendly explanation of each step, refer to **[version1.md](version1.md)**.

Here is the quick sequence:

```bash
# Enter the V1 folder
cd V1

# Step 1: Download & ingest circulars (testing with first 10)
python ingest_one_circular_final_v2.py --limit 10

# Step 2: Create token chunks, embeddings (BGE-M3), and FAISS/BM25 indices
python chunk_embed_16_09_v3.py

# Step 3: Retrieve top chunks & generate answers using Gemini
python gemini_rbi_answer_generation.py

# Step 4: Run evaluation with DeepSeek judge & export Excel report
python agent_router_eval_v2.py
```

---

## 📊 Summary of Outputs

| Output Location | Format | Generated By | Description |
| :--- | :--- | :--- | :--- |
| `V1/pdfs_ingest_1_09_final/` | `.pdf` | Step 1 | Original official RBI circular PDF files. |
| `V1/output_1_09_final/` | `.md` | Step 1 | Structured Markdown documents with metadata headers and tables. |
| `V1/ingest_1_09_final.db` | `.db` (SQLite) | Step 1 | Relational database containing metadata, full text, and audit logs. |
| `V1/output_chunks_16_09_v3/` | `.index` / `.json` | Step 2 | FAISS vector index, chunk metadata, and BM25 index. |
| `V1/gemini_response_generated.json` | `.json` | Step 3 | Generated answers, source citations, and retrieved chunk details. |
| `V1/agentrouter_answer_eval_v2.json` | `.json` | Step 4 | RAGAS evaluation metrics for each benchmark question. |
| `V1/agentrouter_answer_eval_v2.xlsx` | `.xlsx` | Step 4 | Comprehensive 4-sheet Excel report with full chunk text audit. |

---

## 🔬 Evaluation Metrics Explained

Step 4 evaluates the system using the **RAGAS** framework:

1. **Faithfulness**: Measures if the answer is completely grounded in the retrieved circular chunks (0% hallucinations).
2. **Answer Relevancy**: Measures how directly and concisely the answer responds to the specific question asked.
3. **Context Precision**: Evaluates whether the most relevant circular passages were placed at the very top of search results.
4. **Context Recall**: Verifies that the retrieved passages captured all essential facts needed according to the gold standard.

Results are categorized into `PASSED` (score $\ge 0.70$) or `FAILED` and broken down by question difficulty (`Easy`, `Medium`, `Hard`, `Procedure`).

---

## ⚙️ Environment Variables Reference

| Variable | Required | Default | Description |
| :--- | :---: | :--- | :--- |
| `GEMINI_API_KEY` | **Yes** | None | API key for Google Gemini model calls (`gemini-2.5-flash`). |
| `AGENT_ROUTER_API`| **Yes** | None | API key for AgentRouter (`deepseek-v4-flash` evaluation judge). |
| `OPENROUTER_API_KEY`| No | None | Optional fallback key if routing calls through OpenRouter. |
| `RAG_MAX_QUESTIONS`| No | `30` | Number of questions to answer in Step 3 (`0` or unset to process all). |
| `GEMINI_MODEL` | No | `gemini-2.5-flash` | Gemini model variant used for generation. |

---

## 🤝 Contributing & License

Contributions, bug reports, and improvements are welcome! Please open an issue or submit a Pull Request.

Licensed under the [MIT License](LICENSE).
