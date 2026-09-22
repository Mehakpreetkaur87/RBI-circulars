# Local BGE-M3 Embedding Model Setup Guide

## 1. Why a Local `models/` Folder?
When querying or generating embeddings, downloading the model online from Hugging Face every time causes:
- Network timeouts, SSL handshake failures, and Hugging Face rate limits.
- High latency during query retrieval.

Storing the model locally inside `models/bge-m3` ensures **100% offline, zero-cost, and instant embeddings**.

---

## 2. Directory Structure
Ensure your project root contains:
```text
RBI_CIRCULARS/
├── models/
│   └── bge-m3/
│       ├── pytorch_model.bin (or model.safetensors)
│       ├── config.json
│       ├── tokenizer.json
│       ├── tokenizer_config.json
│       └── modules.json
├── Sept1/
└── test_bge_m3_model.py
```

---

## 3. How to Download the Model

### Option A: Using Python (Recommended)
Run this one-liner to download all model files directly into `models/bge-m3`:
```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='BAAI/bge-m3', local_dir='models/bge-m3')"
```

### Option B: Using Git LFS
```bash
git lfs install
git clone https://huggingface.co/BAAI/bge-m3 models/bge-m3
```

---

## 4. Configuration (.env)
Add the path in your `.env` file (optional, scripts default to `models/bge-m3` automatically):
```env
BGE_MODEL_PATH=models/bge-m3
```

---

## 5. Verify the Setup
Test that the local model loads without internet connection:
```bash
python test_bge_m3_model.py
```
**Expected Output:**
```text
Loading BGE-M3 locally...
Creating a test embedding...
SUCCESS: BGE-M3 is working correctly.
Embedding shape : (1, 1024)
```
