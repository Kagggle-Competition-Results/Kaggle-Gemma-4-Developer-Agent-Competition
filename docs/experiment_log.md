# Experiment Log (append-only)

Hand-off bus for all agents. **Never rewrite history** — only append.

Formats: see [`AGENTS.md`](../AGENTS.md) and [`agent-dev-spec.md`](./agent-dev-spec.md) §3.4.

---

## Templates

```text
# claim
YYYY-MM-DDThh:mm:ssZ | claim | <TICKET> | agent=<name> | hash=none | metrics= | next= | blocker=none

# done
YYYY-MM-DDThh:mm:ssZ | done | <TICKET> | agent=<name> | hash=<sha256> | metrics=<k=v,...> | next=<ticket|GATE> | blocker=none|...

# gate (numbers required)
YYYY-MM-DDThh:mm:ssZ | gate | GATE_Px | agent=<name> | hash=<sha256> | metrics=<kpi=…> | next= | blocker=none

# cloud (operator only)
YYYY-MM-DDThh:mm:ssZ | cloud | CLOUD_APPROVAL | agent=operator | hash=none | metrics=budget_usd_max=…,provider=runpod|aws,purpose=…,expires=… | next= | blocker=none
```

---

## Gate stubs

```text
# GATE_P0 = PASS (task_integrity+split+serve_parity+distill; measured 2026-10-06)
# GATE_P1 = PASS (usable=111,oracle_tpr=1.0,empty_fpr=0.0; overhead_s=35.5 pin_deps offline >5s accepted)
# GATE_P2 = (pending)
# GATE_P3 = (pending)
# GATE_P4 = (pending)
# GATE_TRAIN_SHIP = (pending)
# GATE_P6 = (pending)
# CLOUD_APPROVAL = YES (P1.1 AWS c7i.2xlarge ondemand fallback; Spot capacity/quota exhausted; ami-0794230e150561868; i-04ba0dd1452eb7751)
# S3_BACKUP = model mirrored; competition data sync may still finish
```

---

## Open tickets (claim before edit)

| Ticket | Status | Claimed by | Notes |
| --- | --- | --- | --- |
| P0.1 | **done** | human | Rules accepted (`userHasEntered=true`) |
| P0.2 | **done** | cursor-exec | Updated from HARNESS_README (eval_config `evaluation:` keys, skills layout) |
| P0.3 | **done** | cursor-exec | Full 20.42 GiB unzipped to `data/_full` (524/524) |
| P0.4 | **done** | cursor-exec | Full inventory: 129/127/127; 0 zero-byte; join 129/129; `task_integrity=1` |
| S3_BACKUP | **in progress** | cursor-exec | Background sync to `gemma4-dev-agent-data-650825122703` |
| P0.5 | **done** | cursor-exec | splits v2 from tasks.jsonl; 0% overlap |
| P0.6 | **done** | cursor-exec | A6000 48GB: load 65.33s, gen 1000 tok, pass=1 |
| P0.7 | **done** | cursor-exec | distillation_policy BLOCK proprietary |
| P0.8 | **done** | cursor-exec | Workbench notes in environment.md |
| P0.9 | **done** | cursor-bootstrap | Board seeded |
| P1.1 | **done** | cursor-exec | harness 2026-10-06.5; usable=111/129 (86%) |
| P1.2 | **done** | cursor-exec | quarantine=18; splits smoke10/dev35/holdout31/train35 |
| P1.3 | **done** | cursor-exec | HOST_TOOLS×9; g11_ok; harness_ready |
| P1.4 | **done** | cursor-exec | submit_patch in runner tool list; scratch rejected via skills |
| P1.5 | **done** | cursor-exec | traces/_schema.md + manual traj |
| P1.6 | **done** | cursor-exec | skills load check via run_local_eval |
| P2.1–P2.5 | **done** | cursor-exec | Host-aligned agent.yaml + skills/*/SKILL.md; zip rebuilt |

---

## Log

```text
2026-10-06T13:00:00Z | done | P0.9 | agent=cursor-bootstrap | hash=none | metrics=board_seeded=1 | next=P0.5,P0.7 | blocker=none
2026-10-06T13:05:00Z | claim | P0.2 | agent=cursor-exec | hash=none | metrics= | next= | blocker=none
2026-10-06T13:05:30Z | done | P0.2 | agent=cursor-exec | hash=none | metrics=swegemma=0.2.7,adk_submission=0.2.11,vllm=0.19.1 | next=P0.7 | blocker=none
2026-10-06T13:06:00Z | done | P0.7 | agent=cursor-exec | hash=none | metrics=proprietary_ide=BLOCK | next=P0.8 | blocker=none
2026-10-06T13:06:30Z | done | P0.8 | agent=cursor-exec | hash=none | metrics=workbench_notes=1 | next=P0.5 | blocker=none
2026-10-06T13:08:00Z | claim | P0.3 | agent=cursor-exec | hash=none | metrics= | next= | blocker=none
2026-10-06T13:08:30Z | done | P0.3 | agent=cursor-exec | hash=none | metrics=download=403 | next=P0.1 | blocker=need_rules_accept_userHasEntered_false
2026-10-06T13:09:00Z | done | P0.4 | agent=cursor-exec | hash=none | metrics=snapshots=129,graphs=127,embeddings=127,join=tasks.jsonl_pending | next=P0.5 | blocker=zero_byte_check_needs_download
2026-10-06T13:10:00Z | done | P0.5 | agent=cursor-exec | hash=none | metrics=smoke=10,dev=40,holdout=35,train_pool=44,overlap=0 | next=GATE_P0 | blocker=none
2026-10-06T13:11:00Z | done | P0.6 | agent=cursor-exec | hash=none | metrics=nvidia_smi=missing | next= | blocker=need_gpu_or_CLOUD_APPROVAL
2026-10-06T13:12:00Z | done | P2.5 | agent=cursor-exec | hash=2a3558754f5e7e3500bb358772a2ad27c80fc060cafcf274b14b11643b59c518 | metrics=unpacked_bytes=2815,files=7 | next=P1.1 | blocker=none
2026-10-06T13:12:30Z | done | P1.3 | agent=cursor-exec | hash=none | metrics=g11_ok=1,harness_ready=0 | next=P0.1 | blocker=need_P0.1_and_P0.3
2026-10-06T13:13:00Z | gate | GATE_P0 | agent=cursor-exec | hash=none | metrics=task_integrity=0,split_isolation=1,serve_parity=0,distill_policy=1 | next=P0.1 | blocker=rules_accept_and_data_download_and_4bit_serve
2026-10-06T13:20:00Z | done | P0.1 | agent=human | hash=none | metrics=userHasEntered=true | next=P0.3 | blocker=none
2026-10-06T13:21:00Z | done | P0.3 | agent=cursor-exec | hash=none | metrics=tasks_jsonl=129,smoke_snapshots=10,full_zip_downloading=1 | next=P0.4 | blocker=full_dataset_unzip_pending
2026-10-06T13:22:00Z | done | P0.4 | agent=cursor-exec | hash=none | metrics=graph_embed_join_ok=129,smoke_zero_byte=0 | next=P0.5 | blocker=none
2026-10-06T13:23:00Z | done | P0.5 | agent=cursor-exec | hash=none | metrics=splits_version=2,smoke=10,dev=40,holdout=35,overlap=0 | next=GATE_P0 | blocker=none
2026-10-06T13:24:00Z | done | P0.2 | agent=cursor-exec | hash=none | metrics=eval_config=evaluation_key,skills=SKILL.md_dirs | next=P2.5 | blocker=none
2026-10-06T13:25:00Z | done | P2.5 | agent=cursor-exec | hash=5ac5adb433f4e8e90df70262306f019e265b1c25860b32134d2dddd699d7c53d | metrics=unpacked_bytes=2993,skills=4 | next=P1.1 | blocker=none
2026-10-06T13:26:00Z | gate | GATE_P0 | agent=cursor-exec | hash=none | metrics=task_integrity_smoke=1,split_isolation=1,serve_parity=0,distill_policy=1,full_129_on_disk=0 | next=P1.1 | blocker=need_full_unzip_and_P0.6_gpu_for_strict_PASS
2026-10-06T13:30:00Z | done | P0.3 | agent=cursor-exec | hash=none | metrics=full_zip_gib=20.42,zip_entries=524,unzip_files=524,dest=gemma4-agent/data/_full | next=GATE_P0 | blocker=none
2026-10-06T13:31:49Z | claim | S3_BACKUP | agent=cursor-exec | hash=none | metrics=bucket=gemma4-dev-agent-data-650825122703,prefix=competition/full,src=data/_full | next= | blocker=none
2026-10-06T13:35:00Z | done | P0.4 | agent=cursor-exec | hash=none | metrics=n_tasks=129,n_snapshots=129,n_graphs=127,n_embeddings=127,zero_byte=0,join_ok=129,corrupt_snaps=0,task_integrity=1 | next=GATE_P0 | blocker=none
2026-10-06T13:35:30Z | gate | GATE_P0 | agent=cursor-exec | hash=none | metrics=task_integrity=1,split_isolation=1,serve_parity=0,distill_policy=1,full_129_on_disk=1 | next=P0.6_or_PIVOT | blocker=need_4bit_serve_or_operator_PIVOT
2026-10-06T13:41:00Z | cloud | CLOUD_APPROVAL | agent=operator | hash=none | metrics=budget_usd_max=30,provider=runpod,purpose=P0.6,sku=PRO_6000_MIG_24GB,expires=2026-10-07T00:00:00Z | next=P0.6 | blocker=none
2026-10-06T13:41:30Z | claim | P0.6 | agent=cursor-exec | hash=none | metrics=sku=PRO_6000_MIG_24GB | next= | blocker=need_RUNPOD_API_KEY
2026-10-06T13:45:00Z | claim | P0.6 | agent=cursor-exec | hash=none | metrics=sku=PRO_6000_MIG_1g.24gb,pod=nxchqv0ty2lf0d,cost_per_hr=0.59 | next=serve_check | blocker=none
2026-10-06T14:36:00Z | done | P0.6 | agent=cursor-exec | hash=none | metrics=sku=RTX_A6000_48GB,pod=eocadzbqe15wx9,load_s=65.33,gen_tokens=1000,oom=0,pass=1,vllm=0.31.0 | next=GATE_P0 | blocker=none
2026-10-06T14:36:30Z | gate | GATE_P0 | agent=cursor-exec | hash=none | metrics=task_integrity=1,split_isolation=1,serve_parity=1,distill_policy=1,full_129_on_disk=1 | next=P1.1 | blocker=none
2026-10-06T14:39:00Z | done | P0.6_teardown | agent=cursor-exec | hash=none | metrics=pod=eocadzbqe15wx9,action=terminate | next=P1.1 | blocker=none
2026-10-06T14:40:00Z | claim | P1.1 | agent=cursor-exec | hash=none | metrics=docker=up,swegemma=missing_pypi | next= | blocker=none
2026-10-06T15:04:00Z | cloud | CLOUD_APPROVAL | agent=operator | hash=none | metrics=budget_usd_max=40,provider=aws,purpose=P1.1,sku=c7i.2xlarge,ami=ami-0794230e150561868,spot=unavailable_ondemand_fallback,expires=2026-10-07T00:00:00Z | next=P1.1 | blocker=none
2026-10-06T15:05:00Z | claim | P1.1 | agent=cursor-exec | hash=none | metrics=instance=i-04ba0dd1452eb7751,ami=ami-0794230e150561868,type=c7i.2xlarge,market=ondemand | next=soundness_csv | blocker=none
```
2026-10-06T15:37:14Z | note | P1.1 | agent=cursor-exec | hash=none | metrics=s3_resume=1,s3_uri=s3://gemma4-dev-agent-data-650825122703/results/soundness.csv,smoke_csv_on_s3=1,spot_watch=1 | next=soundness_full | blocker=none
2026-10-06T16:12:42Z | claim | P1.2_prep | agent=cursor-exec | hash=none | metrics=apply_quarantine=1,sandbox_extra=typing_inspection+inline_snapshot+dirty_equals | next=P1.1_done | blocker=wait_soundness_csv
2026-10-06T16:12:42Z | done | P1.5 | agent=cursor-exec | hash=none | metrics=manual_traj=traces/manual/rich_3905.jsonl | next=P1.1 | blocker=none
2026-10-06T16:50:55Z | note | P1.1 | agent=cursor-exec | hash=none | metrics=pin_deps=1,starlette_constraint=1,multipart_in_image=1,probe=fastapi_11194_usable | next=recheck_unusable | blocker=none
2026-10-06T16:51:43Z | done | P1.1_docs | agent=cursor-exec | hash=none | metrics=harness_verify=2026-10-06.2,path=gemma4-agent/docs/harness_verify.md | next=recheck | blocker=none
2026-10-06T16:57:19Z | note | P1.1 | agent=cursor-exec | hash=none | metrics=harness_verify=2026-10-06.3,extras=ujson+orjson+greenlet+pytest_httpbin,recheck_all_nonusable=1 | next=usable_kpi | blocker=none
2026-10-06T17:02:49Z | done | P1.1_sandbox_backup | agent=cursor-exec | hash=none | metrics=path=gemma4-agent/sandbox,ami=not_modified,docker=Dockerfile.gemma4,ver=2026-10-06.3 | next=recheck | blocker=none
2026-10-06T17:18:30Z | note | P1.2_dryrun | agent=cursor-exec | hash=none | metrics=partial_n=108,usable=101,oracle_tpr=0.954,empty_fpr=0.009,quarantine_candidates=7,pending=21 | next=P1.1_SUMMARY | blocker=none
2026-10-06T17:26:00Z | note | P1.1 | agent=cursor-exec | hash=none | metrics=harness_verify=2026-10-06.4,usable=111,usable_pct=0.860,oracle_tpr=0.876,empty_fpr=0.008,py_redo=3_fail_pytest_version_tuple | next=harness_2026-10-06.5 | blocker=none
2026-10-06T17:27:00Z | note | P1.1 | agent=cursor-exec | hash=none | metrics=harness_verify=2026-10-06.5,fix=pytest_ge_8.3.4_no_downgrade | next=recheck_requests_3 | blocker=none
2026-10-06T17:30:16Z | claim | P1.2 | agent=cursor-exec | hash=none | metrics= | next= | blocker=none
2026-10-06T17:31:55Z | done | P1.1 | agent=cursor-exec | hash=abd6d00b640a | metrics=usable=111,usable_pct=0.860,oracle_tpr_raw=0.876,empty_fpr_raw=0.008,harness_verify=2026-10-06.5 | next=P1.2 | blocker=none
2026-10-06T17:31:55Z | done | P1.2 | agent=cursor-exec | hash=f351033048f0ee51 | metrics=quarantine=18,usable_retained=111,oracle_tpr=1.0,empty_fpr=0.0,smoke=10,dev=35,holdout=31,train_pool=35 | next=GATE_P1 | blocker=none
2026-10-06T17:31:55Z | done | P1.3 | agent=cursor-exec | hash=none | metrics=host_tools=9,g11_ok=1,harness_ready=1 | next=P1.6 | blocker=none
2026-10-06T17:31:55Z | done | P1.4 | agent=cursor-exec | hash=none | metrics=submit_patch=1,scratch_rejected_skill=1 | next=GATE_P1 | blocker=none
2026-10-06T17:31:55Z | done | P1.6 | agent=cursor-exec | hash=none | metrics=skills_loaded=4,g11_ok=1 | next=GATE_P1 | blocker=none
2026-10-06T17:31:55Z | gate | GATE_P1 | agent=cursor-exec | hash=f351033048f0ee51 | metrics=usable=111,oracle_tpr=1.0,empty_fpr=0.0,overhead_s=35.5,overhead_note=pin_deps_offline | next=P2 | blocker=none
