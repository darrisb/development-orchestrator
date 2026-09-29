#!/usr/bin/env python3
"""Check recoverability for TS-109 RUN 6."""

import json
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DATABASE_URL = "postgresql+psycopg://orchestrator:orchestrator@localhost:5432/orchestrator"

RUN_ID = UUID("23fa67a0-072f-4ca4-8ac9-4ccd903ffaea")
TASK_ID = UUID("006950fe-2cdf-4624-a40a-eb8e2f1c1e1d")
PROJECT_ID = UUID("d98cb1e7-75e4-401a-8b21-d0965b0b3115")

def main():
    try:
        engine = create_engine(DATABASE_URL)
        Session = sessionmaker(bind=engine)
        session = Session()
        
        print("=" * 80)
        print("TS-109 RUN 6 READ-ONLY RECOVERABILITY CHECK")
        print("=" * 80)
        
        # Get run details
        run = session.execute(text("""
            SELECT id, external_run_id, run_number, status, attempt_number, 
                   execution_generation, execution_owner, execution_started_at,
                   candidate_commit, starting_commit, branch_name, review_cycle,
                   created_at, updated_at
            FROM task_runs 
            WHERE id = :run_id
        """), {"run_id": str(RUN_ID)}).fetchone()
        
        if not run:
            print(f"ERROR: Run {RUN_ID} not found")
            return
        
        print("\n--- RUN DETAILS ---")
        print(f"Run ID: {run.id}")
        print(f"External Run ID: {run.external_run_id}")
        print(f"Run Number: {run.run_number}")
        print(f"Status: {run.status}")
        print(f"Attempt Number: {run.attempt_number}")
        print(f"Execution Generation: {run.execution_generation}")
        print(f"Execution Owner: {run.execution_owner}")
        print(f"Execution Started At: {run.execution_started_at}")
        print(f"Candidate Commit: {run.candidate_commit}")
        print(f"Starting Commit: {run.starting_commit}")
        print(f"Branch Name: {run.branch_name}")
        print(f"Review Cycle: {run.review_cycle}")
        
        # Get task details
        task = session.execute(text("""
            SELECT id, external_task_id, status, limits
            FROM tasks 
            WHERE id = :task_id
        """), {"task_id": str(TASK_ID)}).fetchone()
        
        print("\n--- TASK DETAILS ---")
        print(f"Task ID: {task.id}")
        print(f"External Task ID: {task.external_task_id}")
        print(f"Task Status: {task.status}")
        print(f"Task Limits: {task.limits}")
        
        # Get all runs for this task
        runs = session.execute(text("""
            SELECT id, external_run_id, run_number, status, attempt_number, 
                   execution_generation, execution_owner
            FROM task_runs 
            WHERE task_id = :task_id
            ORDER BY run_number
        """), {"task_id": str(TASK_ID)}).fetchall()
        
        print("\n--- ALL RUNS FOR TS-109 ---")
        for r in runs:
            print(f"  RUN #{r.run_number}: {r.external_run_id} - Status: {r.status}, "
                  f"Attempt: {r.attempt_number}, Gen: {r.execution_generation}, "
                  f"Owner: {r.execution_owner}")
        
        # Get model calls for this run
        model_calls = session.execute(text("""
            SELECT id, attempt, purpose, provider, model, status, error_type,
                   duration_ms, response_tokens, prompt_tokens, created_at
            FROM model_calls 
            WHERE task_run_id = :run_id
            ORDER BY attempt, created_at
        """), {"run_id": str(RUN_ID)}).fetchall()
        
        print("\n--- MODEL CALLS FOR RUN 6 ---")
        for mc in model_calls:
            print(f"  Attempt {mc.attempt}: {mc.purpose} - {mc.provider}/{mc.model}")
            print(f"    Status: {mc.status}, Error: {mc.error_type}")
            print(f"    Duration: {mc.duration_ms}ms")
            print(f"    Tokens: prompt={mc.prompt_tokens}, response={mc.response_tokens}")
        
        # Get run events
        events = session.execute(text("""
            SELECT id, event_type, attempt, payload, created_at
            FROM run_events 
            WHERE task_run_id = :run_id
            ORDER BY created_at
        """), {"run_id": str(RUN_ID)}).fetchall()
        
        print("\n--- RUN EVENTS ---")
        for e in events:
            payload = json.dumps(e.payload, indent=2)[:200]
            print(f"  {e.event_type} (attempt {e.attempt}): {payload}...")
        
        # Check workflow checkpoints
        checkpoints = session.execute(text("""
            SELECT checkpoint_id, thread_id, created_at
            FROM workflow_checkpoints 
            WHERE thread_id = :run_id
            ORDER BY checkpoint_id DESC
            LIMIT 5
        """), {"run_id": str(RUN_ID)}).fetchall()
        
        print("\n--- WORKFLOW CHECKPOINTS ---")
        for cp in checkpoints:
            print(f"  {cp.checkpoint_id} - {cp.created_at}")
        
        # Check for attempt 2 evidence
        attempt2_calls = session.execute(text("""
            SELECT COUNT(*) as cnt
            FROM model_calls 
            WHERE task_run_id = :run_id AND attempt >= 2
        """), {"run_id": str(RUN_ID)}).fetchone()
        
        print("\n--- ATTEMPT 2+ EVIDENCE ---")
        print(f"Model calls with attempt >= 2: {attempt2_calls.cnt}")
        
        # Check for edit applications
        edits = session.execute(text("""
            SELECT COUNT(*) as cnt
            FROM code_edits 
            WHERE task_run_id = :run_id
        """), {"run_id": str(RUN_ID)}).fetchone()
        
        print(f"Code edits applied: {edits.cnt}")
        
        # Check for verifications
        verifications = session.execute(text("""
            SELECT COUNT(*) as cnt
            FROM verifications 
            WHERE task_run_id = :run_id
        """), {"run_id": str(RUN_ID)}).fetchone()
        
        print(f"Verifications: {verifications.cnt}")
        
        # Check for reviews
        reviews = session.execute(text("""
            SELECT COUNT(*) as cnt
            FROM reviews 
            WHERE task_run_id = :run_id
        """), {"run_id": str(RUN_ID)}).fetchone()
        
        print(f"Reviews: {reviews.cnt}")
        
        # Check TS-110 state
        ts110_runs = session.execute(text("""
            SELECT id, external_run_id, run_number, status
            FROM task_runs 
            WHERE task_id IN (SELECT id FROM tasks WHERE external_task_id = 'TS-110')
            ORDER BY run_number
        """)).fetchall()
        
        print("\n--- TS-110 STATE ---")
        if ts110_runs:
            for r in ts110_runs:
                print(f"  {r.external_run_id}: {r.status}")
        else:
            print("  No runs found (READY / 0 TaskRuns)")
        
        # Check integration branch
        integration = session.execute(text("""
            SELECT starting_commit
            FROM task_runs 
            WHERE task_id = :task_id AND run_number = 6
        """), {"task_id": str(TASK_ID)}).fetchone()
        
        print("\n--- INTEGRATION ---")
        print("Expected integration: fc6abc579cee88f821c5f72f00162872b2dc8326")
        print(f"Run starting commit: {integration.starting_commit if integration else 'N/A'}")
        
        session.close()
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
