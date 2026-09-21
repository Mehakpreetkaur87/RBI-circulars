"""
================================================================================
RBI CIRCULARS RAG EVALUATION PIPELINE (VERSION 2) - AGENTROUTER APPROACH
File: agent_router_eval_v2.py
================================================================================

Description:
  Version 2 evaluation pipeline using AgentRouter's DeepSeek model (deepseek-v4-flash)
  as the judge for evaluating RAG answer generation quality.
  
Key Enhancements in Version 2:
  1. Primary Mapping with gemini_response_generated.json:
     - Directly evaluates all questions answered in gemini_response_generated.json.
     - Performs a two-way code-based mapping with test_file.json (by question_id,
       positional index, and normalized text).
  2. Direct Side-by-Side Question Comparison:
     - Records both 'original_test_question' (from test_file.json) and
       'generated_file_question' (from gemini_response_generated.json).
     - Computes 'question_text_match': True/False flag to guarantee identity.
  3. Full Retrieved Chunks Preservation (No Information Loss):
     - JSON Output: Stores 'retrieved_chunks_detail' array containing full chunk texts,
       source filenames, chunk numbers, distances, and character counts.
     - Excel Output: Adds a dedicated 4th sheet 'Retrieved_Chunks_Audit' to inspect
       all chunks without cluttering the primary metrics sheet.
  4. Comprehensive Failure/Skip Classification:
     - EVALUATED: Evaluation completed successfully across all metrics.
     - SKIPPED_NOT_GENERATED_YET: Question not yet present in generation checkpoint.
     - SKIPPED_DUE_TO_FAILED_GENERATION: Empty or failed generation answer.
     - SKIPPED_DUE_TO_EMPTY_CHUNKS: Zero chunks retrieved.
     - EVALUATION_FAILED: Network/judge error (exact reason recorded).
  5. AgentRouter Integration & RAGAS 0.4.3 Compatibility:
     - Endpoint: https://agentrouter.org/v1
     - Auth: AGENT_ROUTER_API key from .env
     - Header: 'User-Agent: Cline/3.0.0' for authorized developer gateway access
     - Local Embeddings: BAAI/bge-small-en-v1.5 (offline, 0 credits, no rate limits)
     - Atomic per-item checkpointing (safe against Ctrl+C interruptions).

Outputs:
  - JSON : V1/agentrouter_answer_eval_v2.json
  - Excel: V1/agentrouter_answer_eval_v2.xlsx
================================================================================
"""

import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
import pandas as pd

# ------------------------------------------------------------
# 1. CONFIGURATION & FILE PATHS
# ------------------------------------------------------------
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if not ENV_PATH.exists():
    ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)

AGENT_KEY = os.getenv("AGENT_ROUTER_API")
if not AGENT_KEY:
    raise ValueError(
        "AGENT_ROUTER_API key not found in .env file!\n"
        "Please ensure your .env contains: AGENT_ROUTER_API=sk-..."
    )

BASE_DIR = Path(__file__).resolve().parent

# Input benchmark dataset (400 questions)
TEST_FILE = BASE_DIR / "test_file.json"
if not TEST_FILE.exists():
    TEST_FILE = BASE_DIR / "test.json"

# Input generated answers checkpoint
GEN_FILE = BASE_DIR / "gemini_response_generated.json"
if not GEN_FILE.exists():
    GEN_FILE = BASE_DIR / "response_generated.json"

# Output files (Version 2)
OUTPUT_JSON = BASE_DIR / "agentrouter_answer_eval_v2.json"
OUTPUT_EXCEL = BASE_DIR / "agentrouter_answer_eval_v2.xlsx"

# ------------------------------------------------------------
# USER CONTROLS:
# - EVAL_SCOPE:
#     "GENERATED_ONLY" -> Evaluates all questions in gemini_response_generated.json (Default)
#     "FULL_BENCHMARK" -> Evaluates generated ones and records rest as SKIPPED_NOT_GENERATED_YET
# - MAX_QUESTIONS:
#     Set to 1 for quick test, 5 or 10 for sample batch, or None to evaluate all
# ------------------------------------------------------------
EVAL_SCOPE = "GENERATED_ONLY"
MAX_QUESTIONS: Optional[int] = 25

# Delay between evaluations (in seconds)
EVAL_DELAY_SECONDS = 2.0

from langchain_openai import ChatOpenAI
from langchain_community.embeddings import HuggingFaceEmbeddings
from ragas.llms import LangchainLLMWrapper
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.metrics import faithfulness, answer_relevancy, context_precision, context_recall
from ragas import evaluate, EvaluationDataset, SingleTurnSample


# ------------------------------------------------------------
# 2. CHECKPOINTING & MULTI-SHEET EXCEL EXPORT
# ------------------------------------------------------------
def load_checkpoint(json_path: Path) -> Dict[str, Dict[str, Any]]:
    """Loads existing evaluation results to enable resume capability."""
    if not json_path.exists():
        return {}
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return {item.get("question_id", f"Q{i+1:04d}"): item for i, item in enumerate(data)}
            elif isinstance(data, dict):
                return data
    except Exception as e:
        print(f"  [WARNING] Could not read existing checkpoint: {e}. Starting fresh.")
    return {}


def save_checkpoint(records: Dict[str, Dict[str, Any]], json_path: Path):
    """Atomically saves evaluation records immediately to JSON after each question."""
    temp_path = json_path.with_suffix(".tmp")
    output_list = list(records.values())
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(output_list, f, indent=2, ensure_ascii=False)
        temp_path.replace(json_path)
    except Exception as e:
        print(f"  [ERROR] Failed to save JSON checkpoint: {e}")


def export_excel(records: Dict[str, Dict[str, Any]], excel_path: Path):
    """
    Exports a comprehensive 4-Sheet Excel Workbook:
      Sheet 1: 'Detailed_Evaluation' (Primary metrics, side-by-side questions, status)
      Sheet 2: 'Difficulty_Breakdown' (Aggregated metrics by question difficulty)
      Sheet 3: 'Evaluation_Summary' (Overall system KPIs and mean scores)
      Sheet 4: 'Retrieved_Chunks_Audit' (Full text of all retrieved chunks with metadata)
    """
    if not records:
        return

    output_list = list(records.values())

    # Build primary table (excluding heavy chunk text list for readability)
    table_rows = []
    chunk_audit_rows = []

    for item in output_list:
        row = dict(item)
        chunks_detail = row.pop("retrieved_chunks_detail", [])
        table_rows.append(row)

        # Populate Sheet 4: Retrieved Chunks Audit
        qid = item.get("question_id", "")
        qnum = item.get("question_number", "")
        for c in chunks_detail:
            if isinstance(c, dict):
                chunk_audit_rows.append({
                    "question_id": qid,
                    "question_number": qnum,
                    "chunk_rank": c.get("rank"),
                    "source_file": c.get("source_file"),
                    "chunk_number": c.get("chunk_number"),
                    "distance_score": c.get("distance"),
                    "char_count": c.get("char_count"),
                    "token_count": c.get("token_count"),
                    "chunk_text": c.get("chunk_text", "")
                })
            elif isinstance(c, str):
                chunk_audit_rows.append({
                    "question_id": qid,
                    "question_number": qnum,
                    "chunk_rank": 1,
                    "source_file": "N/A",
                    "chunk_number": "N/A",
                    "distance_score": None,
                    "char_count": len(c),
                    "token_count": None,
                    "chunk_text": c
                })

    df_details = pd.DataFrame(table_rows)
    df_chunks = pd.DataFrame(chunk_audit_rows)

    # Sheet 2: Difficulty Breakdown
    if "difficulty" in df_details.columns and "ragas_score_avg" in df_details.columns:
        diff_summary = df_details.groupby("difficulty").agg(
            total_questions=("question_id", "count"),
            evaluated_count=("evaluation_status", lambda s: (s == "EVALUATED").sum()),
            avg_faithfulness=("faithfulness", "mean"),
            avg_answer_relevancy=("answer_relevancy", "mean"),
            avg_context_precision=("context_precision", "mean"),
            avg_context_recall=("context_recall", "mean"),
            mean_ragas_score=("ragas_score_avg", "mean")
        ).reset_index()
    else:
        diff_summary = pd.DataFrame()

    # Sheet 3: System Summary
    total_q = len(df_details)
    evaluated_q = (df_details["evaluation_status"] == "EVALUATED").sum() if "evaluation_status" in df_details.columns else 0
    passed_q = (df_details["status"] == "PASSED").sum() if "status" in df_details.columns else 0

    mean_faith = df_details["faithfulness"].mean() if "faithfulness" in df_details.columns else 0.0
    mean_relev = df_details["answer_relevancy"].mean() if "answer_relevancy" in df_details.columns else 0.0
    mean_prec = df_details["context_precision"].mean() if "context_precision" in df_details.columns else 0.0
    mean_rec = df_details["context_recall"].mean() if "context_recall" in df_details.columns else 0.0
    mean_avg = df_details["ragas_score_avg"].mean() if "ragas_score_avg" in df_details.columns else 0.0

    system_summary = pd.DataFrame([
        {"Metric": "Pipeline Version", "Value": "Version 2 (AgentRouter + DeepSeek-V4-Flash)"},
        {"Metric": "Total Questions Processed", "Value": total_q},
        {"Metric": "Successfully Evaluated", "Value": evaluated_q},
        {"Metric": "Passed Quality Gate (>= 0.70)", "Value": passed_q},
        {"Metric": "Mean Faithfulness (Factual Consistency)", "Value": round(mean_faith, 4) if pd.notnull(mean_faith) else 0.0},
        {"Metric": "Mean Answer Relevancy", "Value": round(mean_relev, 4) if pd.notnull(mean_relev) else 0.0},
        {"Metric": "Mean Context Precision (Rank Quality)", "Value": round(mean_prec, 4) if pd.notnull(mean_prec) else 0.0},
        {"Metric": "Mean Context Recall (Completeness)", "Value": round(mean_rec, 4) if pd.notnull(mean_rec) else 0.0},
        {"Metric": "Overall System Ragas Mean", "Value": round(mean_avg, 4) if pd.notnull(mean_avg) else 0.0},
        {"Metric": "Timestamp", "Value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    ])

    temp_excel = excel_path.with_suffix(".tmp.xlsx")
    try:
        with pd.ExcelWriter(temp_excel, engine="openpyxl") as writer:
            df_details.to_excel(writer, sheet_name="Detailed_Evaluation", index=False)
            if not diff_summary.empty:
                diff_summary.to_excel(writer, sheet_name="Difficulty_Breakdown", index=False)
            system_summary.to_excel(writer, sheet_name="Evaluation_Summary", index=False)
            if not df_chunks.empty:
                df_chunks.to_excel(writer, sheet_name="Retrieved_Chunks_Audit", index=False)
        temp_excel.replace(excel_path)
    except Exception as e:
        print(f"  [ERROR] Failed to save Excel summary: {e}")


# ------------------------------------------------------------
# 3. MAIN EVALUATION PIPELINE RUNNER
# ------------------------------------------------------------
def main():
    print("=" * 78)
    print(" AGENTROUTER RAG EVALUATION PIPELINE (VERSION 2)")
    print("=" * 78)
    print(f"Judge LLM        : DeepSeek (deepseek-v4-flash)")
    print(f"Endpoint         : https://agentrouter.org/v1")
    print(f"Auth Key         : {AGENT_KEY[:8]}... (length: {len(AGENT_KEY)})")
    print(f"Embedding Model  : Local 'BAAI/bge-small-en-v1.5' (Offline / 0 Cost)")
    print(f"Benchmark File   : {TEST_FILE.name}")
    print(f"Generated Answers: {GEN_FILE.name}")
    print(f"Output JSON      : {OUTPUT_JSON.name}")
    print(f"Output Excel     : {OUTPUT_EXCEL.name}")
    print(f"Evaluation Scope : {EVAL_SCOPE}")
    print(f"Max Questions    : {MAX_QUESTIONS if MAX_QUESTIONS is not None else 'ALL'}")
    print("=" * 78)

    # 1. Initialize AgentRouter LLM
    print("\n[INIT] Connecting to AgentRouter deepseek-v4-flash...")
    llm = ChatOpenAI(
        model="deepseek-v4-flash",
        openai_api_key=AGENT_KEY,
        openai_api_base="https://agentrouter.org/v1",
        default_headers={"User-Agent": "Cline/3.0.0"},  # Authorized client header
        temperature=0.0,
        max_retries=1,
        timeout=45.0
    )

    try:
        ping = llm.invoke('Output JSON: {"ping": "pong"}')
        print(f"  --> AgentRouter connection confirmed! Response: {ping.content.strip()[:60]}")
    except Exception as e:
        print(f"  [ERROR] Failed to connect to AgentRouter: {e}")
        sys.exit(1)

    # 2. Initialize Local Embeddings
    print("\n[INIT] Initializing local BAAI/bge-small-en-v1.5 embeddings...")
    ragas_llm = LangchainLLMWrapper(llm)
    hf_embed = HuggingFaceEmbeddings(model_name="BAAI/bge-small-en-v1.5")
    ragas_embeddings = LangchainEmbeddingsWrapper(hf_embed)

    faithfulness.llm = ragas_llm
    answer_relevancy.llm = ragas_llm
    answer_relevancy.embeddings = ragas_embeddings
    context_precision.llm = ragas_llm
    context_recall.llm = ragas_llm

    # 3. Load Datasets & Build Indices
    print("\n[DATA] Loading benchmark questions and generated answers...")
    with open(TEST_FILE, "r", encoding="utf-8") as f:
        test_questions = json.load(f)

    with open(GEN_FILE, "r", encoding="utf-8") as f:
        gen_data = json.load(f)

    # Index benchmark questions by normalized text and position
    test_by_norm_text = {}
    for idx, tq in enumerate(test_questions, 1):
        q_text = tq.get("question", "").strip().lower()
        if q_text:
            test_by_norm_text[q_text] = (idx, tq)

    # Index generated records by question_id and normalized text
    gen_by_id = {}
    gen_by_norm_text = {}
    for item in gen_data:
        qid = item.get("question_id")
        if qid:
            gen_by_id[qid] = item
        q_text = item.get("question", "").strip().lower()
        if q_text:
            gen_by_norm_text[q_text] = item

    print(f"  Loaded Benchmark Questions: {len(test_questions)}")
    print(f"  Loaded Generated Answers  : {len(gen_data)}")

    # 4. Load Checkpoint (Resume support)
    evaluated_records = load_checkpoint(OUTPUT_JSON)
    print(f"[RESUME] Loaded {len(evaluated_records)} existing evaluation records.")

    # 5. Determine Target Questions
    if EVAL_SCOPE == "GENERATED_ONLY":
        # Target only the questions listed in gemini_response_generated.json
        target_items = gen_data
    else:
        # FULL_BENCHMARK: iterate all 400 questions from test_file.json
        target_items = test_questions

    if MAX_QUESTIONS is not None and MAX_QUESTIONS > 0:
        target_items = target_items[:MAX_QUESTIONS]

    print(f"[RUN] Starting evaluation of {len(target_items)} questions...\n")

    for idx, item in enumerate(target_items, 1):
        if EVAL_SCOPE == "GENERATED_ONLY":
            # Primary item is from gemini_response_generated.json
            gen_rec = item
            gen_q_text = gen_rec.get("question", "").strip()
            qid = gen_rec.get("question_id", f"Q{idx:04d}")
            qnum = gen_rec.get("question_number", idx)
            difficulty = gen_rec.get("difficulty", "Standard")

            # Map to test_file.json for ground truth & side-by-side verification
            norm_q = gen_q_text.lower()
            matched_test = test_by_norm_text.get(norm_q)
            if matched_test:
                orig_qnum, test_rec = matched_test
                orig_test_q = test_rec.get("question", "").strip()
                ground_truth = test_rec.get("ground_truth", "") or gen_rec.get("ground_truth", "")
                ground_truth_ref = test_rec.get("ground_truth_reference", "") or gen_rec.get("ground_truth_reference", "")
            else:
                # Fallback to positional mapping
                if 0 <= (qnum - 1) < len(test_questions):
                    test_rec = test_questions[qnum - 1]
                    orig_test_q = test_rec.get("question", "").strip()
                    ground_truth = test_rec.get("ground_truth", "") or gen_rec.get("ground_truth", "")
                    ground_truth_ref = test_rec.get("ground_truth_reference", "") or gen_rec.get("ground_truth_reference", "")
                else:
                    orig_test_q = gen_q_text
                    ground_truth = gen_rec.get("ground_truth", "")
                    ground_truth_ref = gen_rec.get("ground_truth_reference", "")

        else:
            # Primary item is from test_file.json
            test_rec = item
            orig_test_q = test_rec.get("question", "").strip()
            qnum = idx
            qid = f"Q{idx:04d}"
            difficulty = test_rec.get("difficulty", "Standard")
            ground_truth = test_rec.get("ground_truth", "")
            ground_truth_ref = test_rec.get("ground_truth_reference", "")

            # Match to gemini_response_generated.json
            norm_q = orig_test_q.lower()
            gen_rec = gen_by_id.get(qid) or gen_by_norm_text.get(norm_q)
            gen_q_text = gen_rec.get("question", "").strip() if gen_rec else ""

        # Side-by-side question equality check
        question_matches = (orig_test_q == gen_q_text)

        # Check if already evaluated (Resume Mode)
        if qid in evaluated_records and evaluated_records[qid].get("evaluation_status") == "EVALUATED":
            print(f"[{qid}] Already evaluated. Skipping (Resume Mode).")
            continue

        # Check for ungenerated questions
        if not gen_rec:
            print(f"[{qid}] SKIPPED: Not in generation checkpoint yet.")
            evaluated_records[qid] = {
                "question_id": qid,
                "question_number": qnum,
                "difficulty": difficulty,
                "original_test_question": orig_test_q,
                "generated_file_question": "",
                "question_text_match": False,
                "eval_provider": "AgentRouter",
                "eval_model": "deepseek-v4-flash",
                "evaluation_status": "SKIPPED_NOT_GENERATED_YET",
                "evaluation_reason": f"Answer generation has not been run for this question yet. Awaiting checkpoint.",
                "ground_truth": ground_truth,
                "ground_truth_reference": ground_truth_ref,
                "generated_answer": "",
                "retrieved_chunks_count": 0,
                "retrieved_chunks_detail": [],
                "timestamp": datetime.now().isoformat()
            }
            save_checkpoint(evaluated_records, OUTPUT_JSON)
            export_excel(evaluated_records, OUTPUT_EXCEL)
            continue

        # Extract Answer & Chunks supporting both schemas
        generated_answer = str(gen_rec.get("generated_answer") or gen_rec.get("answer", "")).strip()
        raw_chunks = gen_rec.get("fetched_chunks") or gen_rec.get("retrieved_chunks", [])

        if not generated_answer:
            print(f"[{qid}] SKIPPED: Generated answer is empty.")
            evaluated_records[qid] = {
                "question_id": qid,
                "question_number": qnum,
                "difficulty": difficulty,
                "original_test_question": orig_test_q,
                "generated_file_question": gen_q_text,
                "question_text_match": question_matches,
                "eval_provider": "AgentRouter",
                "eval_model": "deepseek-v4-flash",
                "evaluation_status": "SKIPPED_DUE_TO_FAILED_GENERATION",
                "evaluation_reason": "Answer generation returned empty string or failed.",
                "ground_truth": ground_truth,
                "ground_truth_reference": ground_truth_ref,
                "generated_answer": "",
                "retrieved_chunks_count": len(raw_chunks),
                "retrieved_chunks_detail": raw_chunks,
                "timestamp": datetime.now().isoformat()
            }
            save_checkpoint(evaluated_records, OUTPUT_JSON)
            export_excel(evaluated_records, OUTPUT_EXCEL)
            continue

        # Extract context strings for Ragas
        if raw_chunks and isinstance(raw_chunks[0], dict):
            contexts_list = [c.get("chunk_text", "") for c in raw_chunks if c.get("chunk_text")]
        elif raw_chunks and isinstance(raw_chunks[0], str):
            contexts_list = raw_chunks
        else:
            contexts_list = []

        if not contexts_list:
            print(f"[{qid}] SKIPPED: 0 retrieved chunks available.")
            evaluated_records[qid] = {
                "question_id": qid,
                "question_number": qnum,
                "difficulty": difficulty,
                "original_test_question": orig_test_q,
                "generated_file_question": gen_q_text,
                "question_text_match": question_matches,
                "eval_provider": "AgentRouter",
                "eval_model": "deepseek-v4-flash",
                "evaluation_status": "SKIPPED_DUE_TO_EMPTY_CHUNKS",
                "evaluation_reason": "No context chunks retrieved; cannot evaluate grounding.",
                "ground_truth": ground_truth,
                "ground_truth_reference": ground_truth_ref,
                "generated_answer": generated_answer,
                "retrieved_chunks_count": 0,
                "retrieved_chunks_detail": [],
                "timestamp": datetime.now().isoformat()
            }
            save_checkpoint(evaluated_records, OUTPUT_JSON)
            export_excel(evaluated_records, OUTPUT_EXCEL)
            continue

        print(f"\n--- Evaluating [{qid}] (#{idx}/{len(target_items)}) ---")
        print(f"  Difficulty     : {difficulty}")
        print(f"  Exact Q Match  : {question_matches}")
        print(f"  Question       : {orig_test_q[:90]}...")
        print(f"  Context Chunks : {len(contexts_list)} chunks")

        # Prepare modern SingleTurnSample
        sample = SingleTurnSample(
            user_input=orig_test_q,
            response=generated_answer,
            retrieved_contexts=contexts_list,
            reference=ground_truth if ground_truth else None
        )
        dataset = EvaluationDataset(samples=[sample])
        active_metrics = [faithfulness, answer_relevancy]

        if ground_truth:
            active_metrics.extend([context_precision, context_recall])

        # Execute evaluation
        eval_result_dict = {}
        try:
            t0 = time.time()
            scores = evaluate(
                dataset=dataset,
                metrics=active_metrics,
                raise_exceptions=True
            )
            elapsed_sec = round(time.time() - t0, 2)

            # Safe score extraction
            metric_scores = {}
            if hasattr(scores, "to_pandas"):
                res_df = scores.to_pandas()
                row_dict = res_df.iloc[0].to_dict()
                for m in ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]:
                    if m in row_dict:
                        val = row_dict[m]
                        metric_scores[m] = round(float(val), 4) if pd.notnull(val) else None
            elif hasattr(scores, "scores") and scores.scores:
                metric_scores = dict(scores.scores[0])

            valid_scores = [v for v in metric_scores.values() if v is not None and not pd.isna(v)]
            avg_score = round(sum(valid_scores) / len(valid_scores), 4) if valid_scores else None

            eval_result_dict = {
                "question_id": qid,
                "question_number": qnum,
                "difficulty": difficulty,
                "original_test_question": orig_test_q,
                "generated_file_question": gen_q_text,
                "question_text_match": question_matches,
                "eval_provider": "AgentRouter",
                "eval_model": "deepseek-v4-flash",
                "evaluation_time_sec": elapsed_sec,
                "faithfulness": metric_scores.get("faithfulness"),
                "answer_relevancy": metric_scores.get("answer_relevancy"),
                "context_precision": metric_scores.get("context_precision"),
                "context_recall": metric_scores.get("context_recall"),
                "ragas_score_avg": avg_score,
                "status": "PASSED" if (avg_score is not None and avg_score >= 0.70) else "REVIEW_REQUIRED",
                "evaluation_status": "EVALUATED",
                "evaluation_reason": "Evaluation completed successfully",
                "ground_truth": ground_truth,
                "ground_truth_reference": ground_truth_ref,
                "generated_answer": generated_answer,
                "retrieved_chunks_count": len(contexts_list),
                "retrieved_chunks_detail": raw_chunks,
                "timestamp": datetime.now().isoformat()
            }

            print(f"  [SUCCESS] Evaluated in {elapsed_sec}s | Avg Score: {avg_score}")
            print(f"            Faith: {metric_scores.get('faithfulness')} | Relev: {metric_scores.get('answer_relevancy')} | Prec: {metric_scores.get('context_precision')} | Rec: {metric_scores.get('context_recall')}")

        except Exception as e:
            err_msg = str(e)
            print(f"  [ERROR] Evaluation failed for {qid}: {err_msg[:150]}")
            eval_result_dict = {
                "question_id": qid,
                "question_number": qnum,
                "difficulty": difficulty,
                "original_test_question": orig_test_q,
                "generated_file_question": gen_q_text,
                "question_text_match": question_matches,
                "eval_provider": "AgentRouter",
                "eval_model": "deepseek-v4-flash",
                "evaluation_status": "EVALUATION_FAILED",
                "evaluation_reason": err_msg,
                "ground_truth": ground_truth,
                "ground_truth_reference": ground_truth_ref,
                "generated_answer": generated_answer,
                "retrieved_chunks_count": len(contexts_list),
                "retrieved_chunks_detail": raw_chunks,
                "timestamp": datetime.now().isoformat()
            }

        # Checkpoint immediately after each evaluated question
        evaluated_records[qid] = eval_result_dict
        save_checkpoint(evaluated_records, OUTPUT_JSON)
        export_excel(evaluated_records, OUTPUT_EXCEL)

        # Rate-limiting polite delay
        time.sleep(EVAL_DELAY_SECONDS)

    print("\n" + "=" * 78)
    print(" AGENTROUTER EVALUATION PIPELINE (VERSION 2) COMPLETED")
    print(f" Output JSON : {OUTPUT_JSON}")
    print(f" Output Excel: {OUTPUT_EXCEL}")
    print("=" * 78)


if __name__ == "__main__":
    main()
