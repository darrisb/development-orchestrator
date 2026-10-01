# Recovery Process - Next Steps

## Current Status
- Phase 2 complete: Runtime orchestrator DB untouched and empty
- C73 unreconciled; TS-110 not started; scheduler never invoked
- Manifest verified against surviving artifacts
- All 15 preconditions verified
- 32 required test cases covered
- All validation tests passed (SQLite, PostgreSQL, randomized, Ruff)

## Critical Actions Required (High Priority)

### 1. Final Report Completion (A-AC)
- Complete all remaining items in the final report
- Verify all 5 files are properly committed
- Confirm final commit hash 416bb016ffa1dee30ed8fbfec249f151c25021e9

### 2. C73 Reconciliation Requirements
- Complete conflict-resolved merge before canonical ref advancement
- Ensure human_commit=cbff2c4 recorded
- Verify baseline contains cbff2c4
- Complete operator-resolved path (merge commit onto baseline)
- Phase 3 runtime reconciliation will require a conflict-resolved merge

### 3. Final Commit Validation
- Execute only from repo root
- Verify --confirm-database orchestrator
- Re-verify backup checksum and worker inactivity
- Confirm C73 reconciliation still needed

## Phase 3 Preparation
- Prepare for conflict-resolved merge execution
- Ensure all preconditions met before proceeding
- Validate that operator has reviewed backup checksums

## Key Constraints
- No Git mutations allowed (read-only verb allowlist)
- No scheduler/models/reconciliation imports
- Transaction rollback on any error (zero residue)
- Replay protection in place (two gates)
- Runtime DB must remain empty (head e8a3c7f21d49)
- C73 reconciliation requires conflict resolution before advancement

## Required Verification Points
1. All 15 preconditions verified (DB identity confirmation flag, head==e8a3c7f21d49, campaign tables empty, backup present + SHA-256 + verified metadata + restore checks, repo present, integration==fc6abc5, etc.)
2. Manifest and evidence verified
3. All validation tests passed
4. C73 state gates passed
5. No orchestrator-worker-* containers active
6. All full SHAs resolve
7. All artifacts exist
8. Artifact↔manifest equality verified
9. Limits enforced from YAML
10. No Git mutations (read-only verb allowlist enforced in code)

## Next Steps
1. Complete final report items A-AC
2. Execute C73 reconciliation with conflict resolution
3. Validate final commit requirements
4. Prepare for phase 3 execution with operator verification